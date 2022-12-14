# -*- coding: utf-8 -*-
"""
Created on Tue Nov  8 09:43:22 2022

@author: Ria
"""

"""
    Dedalus script for 3D Quasi-Geostrophic flow.
    This script uses Fourier-bases in the x and y directions and Chebyshev in z.
    The initial conditions are set based on the pressure.
    A LBVP solves for the balanced vertical velocity.
    This script should be ran in parallel, and would be most efficient using a
    2D process mesh.  It uses the built-in analysis framework to save 2D data slices
    in HDF5 files.  The `merge.py` script in this folder can be used to merge
    distributed analysis sets from parallel runs.
"""


import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpi4py import MPI

import time
import logging
logger = logging.getLogger(__name__)

from dedalus import public as de
from dedalus.extras.plot_tools import quad_mesh, pad_limits
from dedalus.extras import flow_tools

Lx, Ly, H  = 80, 40, 1
Nx, Ny, Nz = 64, 32, 8

if MPI.COMM_WORLD.size > (Nx // 2):
    mesh = [(Nx // 2), MPI.COMM_WORLD.size // (Nx // 2)]
else:
    mesh = None

x_basis =   de.Fourier('x', Nx, interval=(0, Lx), dealias=3/2)
y_basis =   de.Fourier('y', Ny, interval=(0, Ly), dealias=3/2)
z_basis = de.Chebyshev('z', Nz, interval=(0, H), dealias=3/2)
domain = de.Domain([x_basis,y_basis,z_basis], np.float64, mesh=mesh)

# Parameters
beta  = 0.1
r     = 0.16
S     = 1.
nu    = 1e-6
kappa = 1e-6

# Background thermal wind
z = domain.grid(2)
U = domain.new_field()
U.meta['x','y']['constant'] = True
U['g'] = S*z

# Vertically-dependent stratification parameter
Gamma = domain.new_field()
Gamma.meta['x','y']['constant'] = True
Gamma['g'] = np.exp(2*(z-1))

# variables: pressure, vertical velocity
problem = de.IVP(domain, variables=['P','W'],ncc_cutoff=1e-8)
problem.meta[:]['z']['dirichlet'] = True

# horizontal velocity perturbations
problem.substitutions['u'] = "-dy(P)"
problem.substitutions['v'] = " dx(P)"

# relative vorticity
problem.substitutions['zeta'] = "dx(v) - dy(u)"

# vertical velocity
problem.substitutions['B'] = "dz(P)"

# advection, Laplacian
problem.substitutions['D(a)'] = "u*dx(a) + v*dy(a)"
problem.substitutions['L(a)'] = "d(a,x=2) + d(a,y=2)"
problem.substitutions['HD(a)'] = "L(L(L(L(a))))"

# parameters
problem.parameters['Gamma'] = Gamma # stratification; must be positive.
problem.parameters['beta']  = beta  # rotation-rate variation
problem.parameters['H']     = H     # height of domain
problem.parameters['r']     = r     # Ekman-friction rate
problem.parameters['U']     = U     # background thermal wind
problem.parameters['nu']    = nu    # viscosity
problem.parameters['kappa'] = kappa # thermal viscosity

# PV
problem.substitutions['PV'] = "zeta + dz(B/Gamma)"

# temperature and vorticity equations
problem.add_equation("dt(zeta) + U*dx(zeta) +  beta*v -    dz(W) +    nu*HD(zeta)  = -D(zeta)")
problem.add_equation("dt(B)    + U*dx(B)    - dz(U)*v + Gamma*W  + kappa*HD(B) = -D(B)")

# match vertical velocity to Ekman layer on the bottom boundary
problem.add_bc(" left(W - r*zeta) = 0")
#problem.add_bc("right(W + r*zeta) = 0", condition="(nx != 0)  or (ny != 0)")
problem.add_bc("right(W) = 0", condition="(nx != 0)  or (ny != 0)")
problem.add_bc("right(P) = 0", condition="(nx == 0) and (ny == 0)")

# Time-stepping solver
solver = problem.build_solver(de.timesteppers.SBDF2)

# Initial dt, and stop conditions
dt_init = 1e-2
solver.stop_iteration = np.inf
solver.stop_sim_time  = 200
solver.stop_wall_time = np.inf

# Initial conditions
x,y,z = domain.grids(scales=domain.dealias)
kz = np.pi / H

P_init, B_init, Q_init = domain.new_fields(3)
for f in [P_init, B_init, Q_init]:
    f.set_scales(domain.dealias)

# Filtered noise.
rand = np.random.RandomState(seed=42)
gshape = domain.dist.grid_layout.global_shape(scales=domain.dealias)[:2]
gslices = domain.dist.grid_layout.slices(scales=domain.dealias)[:2]
noise1 = rand.standard_normal(gshape)[gslices][:,:,None]
noise2 = rand.standard_normal(gshape)[gslices][:,:,None]
P_init['g'] = 30 * (noise1*np.cos(kz*z) + noise2)
P_init.set_scales(1/16)
P_init.require_grid_space()

# Need to solve for W_init using dt(P) --> Pt_init as a slack variable.
init_problem = de.LBVP(domain, variables=['Pt','W'])
init_problem.meta[:]['z']['dirichlet'] = True

init_problem.substitutions = problem.substitutions
init_problem.parameters    = problem.parameters

init_problem.parameters['P'] = P_init
init_problem.parameters['Q'] = Q_init

init_problem.add_equation(" L(Pt) -    dz(W) = -U*dx(zeta) -  beta*v -    nu*HD(zeta)    - D(zeta)")
init_problem.add_equation("dz(Pt) + Gamma*W  = -U*dx(B)    + dz(U)*v - kappa*HD(B) - D(B)")

init_problem.add_bc(" left(W) =   left(r*zeta)")
init_problem.add_bc("right(W)  = 0", condition="(nx != 0)  or (ny != 0)")
init_problem.add_bc("right(Pt) = 0", condition="(nx == 0) and (ny == 0)")

# Init solver
init_solver = init_problem.build_solver()
init_solver.solve()

P = solver.state['P']
W = solver.state['W']
for f in [P,W]: f.set_scales(domain.dealias)

P['g'] = P_init['g']

init_solver.state['W'].set_scales(domain.dealias)
W['g'] = init_solver.state['W']['g']

# Analysis
dumps = solver.evaluator.add_file_handler('snapshots', sim_dt=50, max_writes=1)
dumps.add_task("B")
dumps.add_task("zeta")
dumps.add_task("zeta + dz(B/Gamma)",name='PV')

snap = solver.evaluator.add_file_handler('slices', sim_dt=0.5, max_writes=10)
snap.add_task("interp(W,z=1/2)", name='upwelling-mid', scales=4)
snap.add_task("integ(zeta, 'z')", name='barotropic', scales=4)
snap.add_task("interp(zeta, z=1)", name='vorticity-top', scales=4)
snap.add_task("interp(zeta, x=0)", name='vorticity-xslice', scales=4)
snap.add_task("interp(zeta, y=0)", name='vorticity-yslice', scales=4)
snap.add_task("interp(B, z=1)", name='buoyancy-top', scales=4)
snap.add_task("interp(B, x=0)", name='buoyancy-xslice', scales=4)
snap.add_task("interp(B, y=0)", name='buoyancy-yslice', scales=4)
snap.add_task("interp(PV, z=1)", name='PV-top', scales=4)
snap.add_task("interp(PV, x=0)", name='PV-xslice', scales=4)
snap.add_task("interp(PV, y=0)", name='PV-yslice', scales=4)

traces = solver.evaluator.add_file_handler('traces', sim_dt=0.1, max_writes=1000)
traces.add_task("integ(B*B/Gamma)",   name='PE')
traces.add_task("integ(zeta**2)", name='enstrophy')
traces.add_task("integ(u**2+v**2)",    name='KE')

flow = flow_tools.GlobalFlowProperty(solver, cadence=10)
flow.add_property("sqrt(W*W)", name='W')

CFL = flow_tools.CFL(solver, initial_dt=dt_init, cadence=10, safety=0.25,
                     max_change=1.5, min_change=0.5, max_dt=dt_init, threshold=0.05)
CFL.add_velocity('u',0)
CFL.add_velocity('v',1)

logger.info('Starting loop')
start_run_time = time.time()
while solver.ok:
    if (solver.iteration-1) % 1000 == 0:
        for field in solver.state.fields: field.require_grid_space()
    dt = CFL.compute_dt()
    solver.step(dt)
    if (solver.iteration-1) % 100 == 0:
        logger.info('Iteration: %i, Time: %e, dt: %e' %(solver.iteration, solver.sim_time, dt))
        logger.info('Max W = %f' %flow.max('W'))

end_run_time = time.time()
logger.info('Iterations: %i' %solver.iteration)
logger.info('Sim end time: %f' %solver.sim_time)
logger.info('Run time: %.2f sec' %(end_run_time-start_run_time))
logger.info('Run time: %f cpu-hr' %((end_run_time-start_run_time)/60/60))

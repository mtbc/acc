#!/usr/bin/env python
#
# Copyright (c) 2021 by UT-Battelle, LLC.

from math import ceil, inf, sqrt, tanh
from numba import cuda
from time import time

from mcni.AbstractComponent import AbstractComponent
from mcni.neutron_storage import neutrons_as_npyarr, ndblsperneutron
from mcni.utils.conversion import V2K

category = 'optics'

@cuda.jit(device=True, inline=True)
def calc_reflectivity(Q, R0, Qc, alpha, m, W):
    """
    Calculate the mirror reflectivity for a neutron.

    Returns:
    float: the reflectivity for the neutron's given momentum change
    """
    R = R0
    if Q > Qc:
        R *= (1 - tanh((Q - m * Qc) / W)) * (1 - alpha * (Q - Qc)) / 2
    return R


max_bounces = 100000


@cuda.jit(device=True)
def propagate(
        ww, hh, hw1, hh1, l,
        R0, Qc, alpha, m, W,
        in_neutron
):
    x, y, z, vx, vy, vz = in_neutron[:6]
    t = in_neutron[-2]
    prob = in_neutron[-1]
    # propagate to z=0
    dt = -z/vz
    x += vx*dt; y += vy*dt; z = 0.; t += dt
    # check opening
    if x <= -hw1 or x >= hw1 or y <= -hh1 or y >= hh1:
        in_neutron[-1] = 0
        return
    # bouncing loop
    for nb in range(max_bounces):
        av = l*vx; bv = ww*vz
        ah = l*vy; bh = hh*vz
        vdotn_v1 = bv + av         # Left vertical
        vdotn_v2 = bv - av         # Right vertical
        vdotn_h1 = bh + ah         # Lower horizontal
        vdotn_h2 = bh - ah         # Upper horizontal
        # Compute the dot products of (O - r) and n as c1+c2 and c1-c2 
        cv1 = -hw1*l - z*ww; cv2 = x*l
        ch1 = -hh1*l - z*hh; ch2 = y*l
        # Compute intersection times.
        t1 = (l - z) / vz  # for guide exit
        vdots = (-1., vdotn_v1, vdotn_v2, vdotn_h1, vdotn_h2)
        times = (t1,
                 (cv1 - cv2) / vdotn_v1,
                 (cv1 + cv2) / vdotn_v2,
                 (ch1 - ch2) / vdotn_h1,
                 (ch1 + ch2) / vdotn_h2)
        best_time = inf
        for i in range(0, 5):
            if vdots[i] < 0 and best_time > times[i]:
                best_time = times[i]
                side = i
        if side == 0:
            # Neutron left guide.
            break
        t1 = best_time

        # propagate time t1 to move to reflection point
        x += vx*t1; y += vy*t1; z += vz*t1; t += t1

        # reflection
        if side < 3:
            # left or right vertical mirror
            vdot = (vdotn_v1, vdotn_v2)[side - 1]
            nlen2 = l*l + ww*ww
            q = V2K*(-2)*vdot/sqrt(nlen2)
            d = 2*vdot/nlen2
            vxd = (d*l, -d*l)[side - 1]
            vx = vx - vxd
            vz = vz - d*ww
        else:
            # lower or upper horizontal mirror
            vdot = (vdotn_h1, vdotn_h2)[side - 3]
            nlen2 = l*l + hh*hh
            q = V2K*(-2)*vdot/sqrt(nlen2)
            d = 2*vdot/nlen2
            vyd = (d*l, -d*l)[side - 3]
            vy = vy - vyd
            vz = vz - d*hh
        R = calc_reflectivity(q, R0, Qc, alpha, m, W)
        prob *= R
        if prob <= 0:
            break
    in_neutron[:6] = x, y, z, vx, vy, vz
    in_neutron[-2] = t
    in_neutron[-1] = prob


@cuda.jit
def process_kernel(
        ww, hh, hw1, hh1, l,
        R0, Qc, alpha, m, W,
        neutrons
):
    x = cuda.grid(1)
    if x < len(neutrons):
        propagate(
            ww, hh, hw1, hh1, l,
            R0, Qc, alpha, m, W,
            neutrons[x]
        )
    return


def call_process(
        ww, hh, hw1, hh1, l,
        R0, Qc, alpha, m, W,
        in_neutrons
):
    neutron_count = len(in_neutrons)
    threads_per_block = 512
    number_of_blocks = ceil(neutron_count / threads_per_block)
    print("{} blocks, {} threads".format(number_of_blocks, threads_per_block))
    process_kernel[number_of_blocks, threads_per_block](
        ww, hh, hw1, hh1, l,
        R0, Qc, alpha, m, W,
        in_neutrons
    )
    cuda.synchronize()


class Guide(AbstractComponent):

    def __init__(
            self, name,
            w1, h1, w2, h2, l,
            R0=0.99, Qc=0.0219, alpha=6.07, m=2, W=0.003):
        """
        Initialize this Guide component.
        The guide is centered on the z-axis with the entrance at z=0.

        Parameters:
        name (str): the name of this component
        w1 (m): width at the guide entry
        h1 (m): height at the guide entry
        w2 (m): width at the guide exit
        h2 (m): height at the guide exit
        l (m): length of guide
        R0: low-angle reflectivity
        Qc: critical scattering vector
        alpha: slope of reflectivity
        m: m-value of material (0 is complete absorption)
        W: width of supermirror cutoff
        """
        self.name = name
        ww = .5*(w2-w1); hh = .5*(h2 - h1)
        hw1 = 0.5*w1; hh1 = 0.5*h1
        self._params = (
            float(ww), float(hh), float(hw1), float(hh1), float(l),
            float(R0), float(Qc), float(alpha), float(m), float(W),
        )

        # Aim a neutron at the side of this guide to cause JIT compilation.
        import mcni
        velocity = ((w1 + w2) / 2, 0, l / 2)
        neutrons = mcni.neutron_buffer(1)
        neutrons[0] = mcni.neutron(r=(0, 0, 0), v=velocity, prob=1, time=0)
        self.process(neutrons)

    def process(self, neutrons):
        """
        Propagate a buffer of particles through this guide.
        Adjusts the buffer to include only the particles that exit,
        at the moment of exit.

        Parameters:
        neutrons: a buffer containing the particles
        """
        t1 = time()
        neutron_array = neutrons_as_npyarr(neutrons)
        neutron_array.shape = -1, ndblsperneutron
        t2 = time()
        call_process(*self._params, neutron_array)
        t3 = time()
        good = neutron_array[:, -1] > 0
        neutrons.resize(int(good.sum()), neutrons[0])
        neutrons.from_npyarr(neutron_array[good])
        t4 = time()
        print("prepare input array: ", t2-t1)
        print("call_process: ", t3-t2)
        print("prepare output neutrons: ", t4-t3)
        return neutrons

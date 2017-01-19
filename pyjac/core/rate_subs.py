# -*- coding: utf-8 -*-
"""Module for writing species/reaction rate subroutines.

This is kept separate from Jacobian creation module in order
to create only the rate subroutines if desired.
"""

# Python 2 compatibility
from __future__ import division
from __future__ import print_function

# Standard libraries
import sys
import math
import os
import re
import logging
from string import Template
import textwrap
from collections import OrderedDict

# Non-standard librarys
import sympy as sp
import loopy as lp
import numpy as np
from loopy.kernel.data import temp_var_scope as scopes
from ..loopy import loopy_utils as lp_utils

# Local imports
from .. import utils
from . import chem_model as chem
from . import mech_interpret as mech
from . import CUDAParams
from . import cache_optimizer as cache
from . import mech_auxiliary as aux
from . import shared_memory as shared
from . import file_writers as filew
from ..sympy import sympy_utils as sp_utils
from . reaction_types import reaction_type, falloff_form, thd_body_type


def rxn_rate_const(A, b, E):
    r"""Returns line with reaction rate calculation (after = sign).

    Parameters
    ----------
    A : float
        Arrhenius pre-exponential coefficient
    b : float
        Arrhenius temperature exponent
    E : float
        Arrhenius activation energy

    Returns
    -------
    line : str
        String with expression for reaction rate.

    Notes
    -----
    Form of the reaction rate constant (from, e.g., Lu and Law [1]_):

    .. math::
        :nowrap:

        k_f = \begin{cases}
        A & \text{if } \beta = 0 \text{ and } T_a = 0 \\
        \exp \left( \log A + \beta \log T \right) &
        \text{if } \beta \neq 0 \text{ and } \text{if } T_a = 0 \\
        \exp \left( \log A + \beta \log T - T_a / T \right)	&
        \text{if } \beta \neq 0 \text{ and } T_a \neq 0 \\
        \exp \left( \log A - T_a / T \right)
        & \text{if } \beta = 0 \text{ and } T_a \neq 0 \\
        A \prod^b T	& \text{if } T_a = 0 \text{ and }
        b \in \mathbb{Z} \text{ (integers) }
        \end{cases}

    References
    ----------

    .. [1] TF Lu and CK Law, "Toward accommodating realistic fuel chemistry
       in large-scale computations," Progress in Energy and Combustion
       Science, vol. 35, pp. 192-215, 2009. doi:10.1016/j.pecs.2008.10.002

    """

    line = ''

    if A > 0:
        logA = math.log(A)

        if not E:
            # E = 0
            if not b:
                # b = 0
                line += str(A)
            else:
                # b != 0
                if isinstance(b, int):
                    line += str(A)
                    for i in range(b):
                        line += ' * T'
                else:
                    line += 'exp({:.16e}'.format(logA)
                    if b > 0:
                        line += ' + ' + str(b)
                    else:
                        line += ' - ' + str(abs(b))
                    line += ' * logT)'
        else:
            # E != 0
            if not b:
                # b = 0
                line += 'exp({:.16e}'.format(logA) + ' - ({:.16e} / T))'.format(E)
            else:
                # b!= 0
                line += 'exp({:.16e}'.format(logA)
                if b > 0:
                    line += ' + ' + str(b)
                else:
                    line += ' - ' + str(abs(b))
                line += ' * logT - ({:.16e} / T))'.format(E)
    elif A < 0:
        #a < 0, can't take the log of it
        #the reaction, should also be a duplicate to make any sort of sense
        if not E:
            #E = 0
            if not b:
                #b = 0
                line += str(A)
            else:
                #b != 0
                if utils.is_integer(b):
                    line += str(A)
                    for i in range(int(b)):
                        line += ' * T'
                else:
                    line += '{:.16e} * exp('.format(A)
                    if b > 0:
                        line += str(b)
                    else:
                        line += '-' + str(abs(b))
                    line += ' * logT)'
        else:
            #E != 0
            if not b:
                # b = 0
                line += '{:.16e} * exp(-({:.16e} / T))'.format(A, E)
            else:
                # b!= 0
                line += '{:.16e} * exp('.format(A)
                if b > 0:
                    line += str(b)
                else:
                    line += '-' + str(abs(b))
                line += ' * logT - ({:.16e} / T))'.format(E)

    else:
      raise NotImplementedError

    return line


def get_cheb_rate(lang, rxn, write_defns=True):
    """
    Given a reaction, and a temperature and pressure, this routine
    will generate code to evaluate the Chebyshev rate efficiently.

    Note
    ----
    Assumes existence of variables dot_prod* of sized at least rxn.cheb_n_temp
    Pred and Tred, T and pres, and kf, cheb_temp_0 and cheb_temp_1.

    Parameters
    ----------
    lang : str
        Programming language
    rxn : `ReacInfo`
        Reaction with Chebyshev pressure dependence.
    write_defns : bool, optional
        If ``True`` (default), write calculation of ``Tred`` and ``Pred``.

    Returns
    -------
    line : list of `str`
        Line with evaluation of Chebyshev reaction rate.

    """

    line_list = []
    tlim_inv_sum = 1.0 / rxn.cheb_tlim[0] + 1.0 / rxn.cheb_tlim[1]
    tlim_inv_sub = 1.0 / rxn.cheb_tlim[1] - 1.0 / rxn.cheb_tlim[0]
    if write_defns:
        line_list.append(
                'Tred = ((2.0 / T) - ' +
                '{:.8e}) / {:.8e}'.format(tlim_inv_sum, tlim_inv_sub)
                )

    plim_log_sum = (math.log10(rxn.cheb_plim[0]) +
                    math.log10(rxn.cheb_plim[1])
                    )
    plim_log_sub = (math.log10(rxn.cheb_plim[1]) -
                    math.log10(rxn.cheb_plim[0])
                    )
    if write_defns:
        line_list.append(
                'Pred = (2.0 * log10(pres) - ' +
                '{:.8e}) / {:.8e}'.format(plim_log_sum, plim_log_sub)
                )

    line_list.append('cheb_temp_0 = 1')
    line_list.append('cheb_temp_1 = Pred')
    #start pressure dot product
    for i in range(rxn.cheb_n_temp):
        line_list.append(utils.get_array(lang, 'dot_prod', i) +
          '= {:.8e} + Pred * {:.8e}'.format(rxn.cheb_par[i, 0],
            rxn.cheb_par[i, 1]))

    #finish pressure dot product
    update_one = True
    for j in range(2, rxn.cheb_n_pres):
        if update_one:
            new = 1
            old = 0
        else:
            new = 0
            old = 1
        line = 'cheb_temp_{}'.format(old)
        line += ' = 2 * Pred * cheb_temp_{}'.format(new)
        line += ' - cheb_temp_{}'.format(old)
        line_list.append(line)
        for i in range(rxn.cheb_n_temp):
            line_list.append(utils.get_array(lang, 'dot_prod', i)  +
              ' += {:.8e} * cheb_temp_{}'.format(
                rxn.cheb_par[i, j], old))

        update_one = not update_one

    line_list.append('cheb_temp_0 = 1')
    line_list.append('cheb_temp_1 = Tred')
    #finally, do the temperature portion
    line_list.append('kf = ' + utils.get_array(lang, 'dot_prod', 0) +
                     ' + Tred * ' + utils.get_array(lang, 'dot_prod', 1))

    update_one = True
    for i in range(2, rxn.cheb_n_temp):
        if update_one:
            new = 1
            old = 0
        else:
            new = 0
            old = 1
        line = 'cheb_temp_{}'.format(old)
        line += ' = 2 * Tred * cheb_temp_{}'.format(new)
        line += ' - cheb_temp_{}'.format(old)
        line_list.append(line)
        line_list.append('kf += ' + utils.get_array(lang, 'dot_prod', i) +
                         ' * ' + 'cheb_temp_{}'.format(old))

        update_one = not update_one

    line_list.append('kf = ' + utils.exp_10_fun[lang] + 'kf)')
    line_list = [utils.line_start + line + utils.line_end[lang] for
                  line in line_list]

    return ''.join(line_list)

def assign_rates(reacs, specs, rate_spec):
    """
    From a given set of reactions, determine the rate types for evaluation

    Parameters
    ----------
    reacs : list of `ReacInfo`
        The reactions in the mechanism
    specs : list of `SpecInfo`
        The species in the mechanism
    rate_spec : `RateSpecialization` enum
        The specialization option specified

    Notes
    -----

    For simple Arrhenius evaluations, the rate type keys are:

    if rate_spec == RateSpecialization.full
        0 -> kf = A
        1 -> kf = A * T * T * T ...
        2 -> kf = exp(logA + b * logT)
        3 -> kf = exp(logA - Ta / T)
        4 -> kf = exp(logA + b * logT - Ta / T)

    if rate_spec = RateSpecialization.hybrid
        0 -> kf = A
        1 -> kf = A * T * T * T ...
        2 -> kf = exp(logA + b * logT - Ta / T)

    if rate_spec == lp_utils.rate_specialization.fixed
        0 -> kf = exp(logA + b * logT - Ta / T)

    Returns
    -------
    rate_info : dict of parameters
        Keys are 'simple', 'plog', 'cheb', 'fall', 'chem', 'thd'
        Values are further dictionaries including addtional rate info, number,
        offset, maps, etc.

    Notes
    -----
        Note that the reactions in 'fall', 'chem' and 'thd' are also in 'simple'
        Further, there are duplicates between 'thd' and 'fall' / 'chem'
    """

    #determine specialization
    full = rate_spec == lp_utils.RateSpecialization.full
    hybrid = rate_spec == lp_utils.RateSpecialization.hybrid
    fixed = rate_spec == lp_utils.RateSpecialization.fixed

    #find fwd / reverse rate parameters
    #first, the number of each
    rev_map = [i for i, x in enumerate(reacs) if x.rev]
    num_rev = len(rev_map)
    #next, find the species / nu values
    fwd_spec = []
    fwd_num_spec = []
    fwd_nu = []
    rev_spec = []
    rev_num_spec = []
    rev_nu = []
    fwd_allnu_integer = True
    rev_allnu_integer = True
    for rxn in reacs:
        #fwd
        fwd_spec.extend(rxn.reac[:])
        fwd_num_spec.append(len(rxn.reac))
        fwd_nu.extend(rxn.reac_nu[:])
        #and rev
        rev_spec.extend(rxn.prod[:])
        rev_num_spec.append(len(rxn.prod))
        rev_nu.extend(rxn.prod_nu[:])
    #create numpy versions
    fwd_spec = np.array(fwd_spec, dtype=np.int32)
    fwd_num_spec = np.array(fwd_num_spec, dtype=np.int32)
    if any(not utils.is_integer(nu) for nu in fwd_nu):
        fwd_nu = np.array(fwd_nu)
        fwd_allnu_integer = False
    else:
        fwd_nu = np.array(fwd_nu, dtype=np.int32)
    rev_spec = np.array(rev_spec, dtype=np.int32)
    rev_num_spec = np.array(rev_num_spec, dtype=np.int32)
    if any(not utils.is_integer(nu) for nu in rev_nu):
        rev_nu = np.array(rev_nu)
        fwd_allnu_integer = False
    else:
        rev_nu = np.array(rev_nu, dtype=np.int32)

    def __seperate(reacs, matchers):
        #find all reactions / indicies that match this offset
        rate = [(i, x) for i, x in enumerate(reacs) if any(x.match(y) for y in matchers)]
        mapping = []
        num = 0
        if rate:
            mapping, rate = zip(*rate)
            mapping = np.array(mapping, dtype=np.int32)
            rate = list(rate)
            num = len(rate)

        return rate, mapping, num

    #count / seperate reactions with simple arrhenius rates
    simple_rate, simple_map, num_simple = __seperate(
        reacs, [reaction_type.elementary, reaction_type.thd,
                    reaction_type.fall, reaction_type.chem])

    def __specialize(rates, fall=False):
        fall_types = None
        num = len(rates)
        rate_type = np.zeros((num,), dtype=np.int32)
        if fall:
            fall_types = np.zeros((num,), dtype=np.int32)
        #reaction parameters
        A = np.zeros((num,), dtype=np.float64)
        b = np.zeros((num,), dtype=np.float64)
        Ta = np.zeros((num,), dtype=np.float64)

        for i, reac in enumerate(rates):
            if (reac.high or reac.low) and fall:
                if reac.high:
                    Ai, bi, Tai = reac.high
                    fall_types[i] = 1 #mark as chemically activated
                else:
                    #we want k0, hence default factor is fine
                    Ai, bi, Tai = reac.low
                    fall_types[i] = 0 #mark as falloff
            else:
                #assign rate params
                Ai, bi, Tai = reac.A, reac.b, reac.E
            #generic assign
            A[i] = np.log(Ai)
            b[i] = bi
            Ta[i] = Tai

            if fixed:
                rate_type[i] = 0
                continue
            #assign rate types
            if bi == 0 and Tai == 0:
                A[i] = Ai
                rate_type[i] = 0
            elif bi == int(bi) and bi and Tai == 0:
                A[i] = Ai
                rate_type[i] = 1
            elif Tai == 0 and bi != 0:
                rate_type[i] = 2
            elif bi == 0 and Tai != 0:
                rate_type[i] = 3
            else:
                rate_type[i] = 4
            if not full:
                rate_type[i] = rate_type[i] if rate_type[i] <= 1 else 2
        return rate_type, A, b, Ta, fall_types

    simple_rate_type, A, b, Ta, _ = __specialize(simple_rate)

    #finally determine the advanced rate evaulation types
    plog_reacs, plog_map, num_plog = __seperate(
        reacs, [reaction_type.plog])

    #create the plog arrays
    num_pressures = []
    plog_params = []
    for p in plog_reacs:
        num_pressures.append(len(p.plog_par))
        plog_params.append(p.plog_par)
    num_pressures = np.array(num_pressures, dtype=np.int32)

    cheb_reacs, cheb_map, num_cheb = __seperate(
        reacs, [reaction_type.cheb])

    #create the chebyshev arrays
    cheb_n_pres = []
    cheb_n_temp = []
    cheb_plim = []
    cheb_tlim = []
    cheb_coeff = []
    for cheb in cheb_reacs:
        cheb_n_pres.append(cheb.cheb_n_pres)
        cheb_n_temp.append(cheb.cheb_n_temp)
        cheb_coeff.append(cheb.cheb_par)
        cheb_plim.append(cheb.cheb_plim)
        cheb_tlim.append(cheb.cheb_tlim)

    #find falloff types
    fall_reacs, fall_map, num_fall = __seperate(
        reacs, [reaction_type.fall, reaction_type.chem])
    fall_rate_type, fall_A, fall_b, fall_Ta, fall_types = __specialize(fall_reacs, True)
    #find blending type
    blend_type = np.array([next(int(y) for y in x.type if isinstance(
        y, falloff_form)) for x in fall_reacs], dtype=np.int32)
    #seperate parameters based on blending type
    #sri
    sri_map = np.where(blend_type == int(falloff_form.sri))[0].astype(dtype=np.int32)
    sri_reacs = [reacs[fall_map[i]] for i in sri_map]
    sri_par = [reac.sri_par for reac in sri_reacs]
    #now fill in defaults as needed
    for par_set in sri_par:
        if len(par_set) != 5:
            par_set.extend([1, 0])
    if len(sri_par):
        sri_a, sri_b, sri_c, sri_d, sri_e = [np.array(x, dtype=np.float64) for x in zip(*sri_par)]
    else:
        sri_a, sri_b, sri_c, sri_d, sri_e = [np.empty(shape=(0,)) for i in range(5)]
    #and troe
    troe_map = np.where(blend_type == int(falloff_form.troe))[0].astype(dtype=np.int32)
    troe_reacs = [reacs[fall_map[i]] for i in troe_map]
    troe_par = [reac.troe_par for reac in troe_reacs]
    #now fill in defaults as needed
    for par_set in troe_par:
        if len(par_set) != 4:
            par_set.append(0)
    troe_a, troe_T3, troe_T1, troe_T2 = [np.array(x, dtype=np.float64) for x in zip(*troe_par)]

    #find third-body types
    thd_reacs, thd_map, num_thd = __seperate(
        reacs, [reaction_type.fall, reaction_type.chem, reaction_type.thd])
    #find third body type
    thd_type = np.array([next(int(y) for y in x.type if isinstance(
        y, thd_body_type)) for x in thd_reacs], dtype=np.int32)
    #find the species indicies
    thd_spec_num = []
    thd_spec = []
    thd_eff = []
    for x in thd_reacs:
        if x.match(thd_body_type.species):
            thd_spec_num.append(1)
            thd_spec.append(x.pdep_sp)
            thd_eff.append(1)
        elif x.match(thd_body_type.unity):
            thd_spec_num.append(0)
        else:
            thd_spec_num.append(len(x.thd_body_eff))
            spec, eff = zip(*x.thd_body_eff)
            thd_spec.extend(spec)
            thd_eff.extend(eff)
    thd_spec_num = np.array(thd_spec_num, dtype=np.int32)
    thd_spec = np.array(thd_spec, dtype=np.int32)
    thd_eff = np.array(thd_eff, dtype=np.float64)

    return {'simple' : {'A' : A, 'b' : b, 'Ta' : Ta, 'type' : simple_rate_type,
                'num' : num_simple, 'map' : simple_map},
            'plog' : {'map' : plog_map, 'num' : num_plog,
            'num_P' : num_pressures, 'params' : plog_params},
            'cheb' : {'map' : cheb_map, 'num' : num_cheb,
                'num_P' : cheb_n_pres, 'num_T' : cheb_n_temp,
                'params' : cheb_coeff, 'Tlim' : cheb_tlim,
                'Plim' : cheb_plim},
            'fall' : {'map' : fall_map, 'num' : num_fall,
                'ftype' : fall_types, 'blend' : blend_type,
                'A' : fall_A, 'b' : fall_b, 'Ta' : fall_Ta,
                'type' : fall_rate_type,
                'sri' :
                    {'map' : sri_map,
                     'num' : sri_map.size,
                     'a' : sri_a,
                     'b' : sri_b,
                     'c' : sri_c,
                     'd' : sri_d,
                     'e' : sri_e
                    },
                 'troe' :
                    {'map' : troe_map,
                     'num' : troe_map.size,
                     'a' : troe_a,
                     'T3' : troe_T3,
                     'T1' : troe_T1,
                     'T2' : troe_T2
                    }
                },
            'thd' : {'map' : thd_map, 'num' : num_thd,
                'type' : thd_type, 'spec_num' : thd_spec_num,
                'spec' : thd_spec, 'eff' : thd_eff},
            'fwd' : {'map' : np.arange(len(reacs)), 'num' : len(reacs),
                'num_spec' :  fwd_num_spec, 'specs' : fwd_spec,
                'nu' : fwd_nu, 'allint' : fwd_allnu_integer},
            'rev' : {'map' : rev_map, 'num' : num_rev,
                'num_spec' :  rev_num_spec, 'specs' : rev_spec,
                'nu' : rev_nu, 'allint' : rev_allnu_integer},
            'Nr' : len(reacs),
            'Ns' : len(specs)}

class rateconst_info(object):
    def __init__(self, name, instructions, pre_instructions=[],
        reac_ind='i', kernel_data=None,
        maps=[], extra_inames=[], indicies=[],
        assumptions=[], parameters={}):
        self.name = name
        self.instructions = instructions
        self.pre_instructions = pre_instructions[:]
        self.reac_ind = reac_ind
        self.kernel_data = kernel_data[:]
        self.maps = maps[:]
        self.extra_inames = extra_inames[:]
        self.indicies = indicies[:]
        self.assumptions = assumptions[:]
        self.parameters = parameters.copy()

__TINV_PREINST_KEY = 'Tinv'
__TLOG_PREINST_KEY = 'logT'
__PLOG_PREINST_KEY = 'logP'

def __handle_indicies(indicies, reac_ind, out_map, kernel_data,
                        outmap_name='out_map', alternate_indicies=None,
                        force_zero=False, force_map=False):
    """Consolidates the commonly used indicies mapping steps

    Parameters
    ----------
    indicies: :class:`numpy.ndarray`
        The list of indicies
    reac_ind : str
        The reaction index variable (used in mapping)
    out_map : dict
        The dictionary to store the mapping result in (if any)
    kernel_data : list of :class:`loopy.KernelArgument`
        The data to pass to the kernel (may be added to)
    outmap_name : str, optional
        The name to use in mapping
    alternate_indicies : :class:`numpy.ndarray`
        An alternate list of indicies that can be substituted in to the mapping
    force_zero : bool
        If true, any indicies that don't start with zero require a map (e.g. for
            smaller arrays)
    force_map : bool
        If true, forces use of a map
    Returns
    -------
    indicies : :class:`numpy.ndarray` OR tuple of int
        The transformed indicies
    """

    check = indicies if alternate_indicies is None else alternate_indicies
    if check[0] + check.size - 1 == check[-1] and \
            (not force_zero or check[0] == 0) and \
            not force_map:
        #if the indicies are contiguous, we can get away with an
        check = (check[0], check[0] + check.size)
    else:
        #need an output map
        out_map[reac_ind] = outmap_name
        #add to kernel data
        outmap_lp = lp.TemporaryVariable(outmap_name,
            shape=lp.auto,
            initializer=check.astype(dtype=np.int32),
            read_only=True, scope=scopes.PRIVATE)
        kernel_data.append(outmap_lp)

    return check

def __1Dcreator(name, numpy_arg, index='${reac_ind}', scope=scopes.PRIVATE):
    """
    Simple convenience method for creating 1D loopy arrays from
    Numpy args

    Parameters
    ----------
    name : str
        The loopy arg name
    numpy_arg : :class:`numpy.ndarray`
        The numpy array to use as initializer
    index : str, optional
        The string form of the index used in loopy code. Defaults to ${reac_ind}
    scope : :class:`loopy.temp_var_scope`
        The scope to use for the temporary variable. Defaults to PRIVATE
    """
    arg_lp = lp.TemporaryVariable(name, shape=numpy_arg.shape, initializer=numpy_arg,
                                  scope=scope, dtype=numpy_arg.dtype, read_only=True)
    arg_str = name + '[{}]'.format(index)
    return arg_lp, arg_str

def get_thd_body_concs(eqs, loopy_opt, rate_info, test_size=None):
    """Generates instructions, kernel arguements, and data for third body concentrations

    Parameters
    ----------
    eqs : dict
        Sympy equations / variables for constant pressure / constant volume systems
    loopy_opt : `loopy_options` object
        A object containing all the loopy options to execute
    rate_info : dict
        The output of :method:`assign_rates` for this mechanism
    test_size : int
        If not none, this kernel is being used for testing.
        Hence we need to size the arrays accordingly

    Returns
    -------
    rate_list : list of :class:`rateconst_info`
        The generated infos for feeding into the kernel generator
    """

    Ns = rate_info['Ns']
    num_thd = rate_info['thd']['num']
    reac_ind = 'i'

    #create args
    indicies = ['${species_ind}', 'j']
    last_index = '${species_ind}'
    if loopy_opt.width:
        last_index = 'j'
    concs_lp, concs_str, _ = lp_utils.get_loopy_arg('conc',
                                                    indicies=indicies,
                                                    dimensions=(Ns, test_size),
                                                    order=loopy_opt.order)
    concs_str = Template(concs_str)

    indicies = ['${reac_ind}', 'j']
    last_index = '${reac_ind}'
    if loopy_opt.width:
        last_index = 'j'
    thd_lp, thd_str, _ = lp_utils.get_loopy_arg('thd_conc',
                                                indicies=indicies,
                                                dimensions=(num_thd, test_size),
                                                order=loopy_opt.order)

    T_arr = lp.GlobalArg('T_arr', shape=(test_size,), dtype=np.float64)
    P_arr = lp.GlobalArg('P_arr', shape=(test_size,), dtype=np.float64)

    thd_eff_ns = np.ones(num_thd)
    num_specs = rate_info['thd']['spec_num'].copy()
    spec_list = rate_info['thd']['spec'].copy()
    thd_effs = rate_info['thd']['eff'].copy()

    last_spec = Ns - 1
    #first, we must do some surgery to get _our_ form of the thd-body efficiencies
    for i in range(num_specs.size):
        num = num_specs[i]
        offset = np.sum(num_specs[:i])
        #check if Ns has a non-default efficiency
        if last_spec in spec_list[offset:offset+num]:
            ind = np.where(spec_list[offset:offset+num] == last_spec)[0][0]
            #set the efficiency
            thd_eff_ns[i] = thd_effs[offset + ind]
            #delete from the species list
            spec_list = np.delete(spec_list, offset + ind)
            #delete from effiency list
            thd_effs = np.delete(thd_effs, offset + ind)
            #subtract from species num
            num_specs[i] -= 1
        #and subtract from efficiencies
        thd_effs[offset:offset+num_specs[i]] -= thd_eff_ns[i]
        if thd_eff_ns[i] != 1:
            #we need to add all the other species :(
            #get updated species list
            to_add = np.array(range(Ns - 1), dtype=np.int32)
            #and efficiencies
            eff = np.array([1 - thd_eff_ns[i] for spec in range(Ns - 1)])
            eff[spec_list[offset:offset+num_specs[i]]] = thd_effs[offset:offset+num_specs[i]]
            #delete from the species list / efficiencies
            spec_list = np.delete(spec_list, range(offset, offset+num_specs[i]))
            thd_effs = np.delete(thd_effs,  range(offset, offset+num_specs[i]))
            #insert
            spec_list = np.insert(spec_list, offset, to_add)
            thd_effs = np.insert(thd_effs, offset, eff)
            #and update number
            num_specs[i] = to_add.size


    #and temporary variables:
    thd_type_lp, thd_type_str = __1Dcreator('thd_type', rate_info['thd']['type'], scope=scopes.GLOBAL)
    thd_eff_lp, thd_eff_str = __1Dcreator('thd_eff', thd_effs)
    thd_spec_lp, thd_spec_str = __1Dcreator('thd_spec', spec_list)
    thd_num_spec_lp, thd_num_spec_str = __1Dcreator('thd_spec_num', num_specs)
    thd_eff_ns_lp, thd_eff_ns_str = __1Dcreator('thd_eff_ns', thd_eff_ns)

    #calculate offsets
    offsets = np.cumsum(num_specs, dtype=np.int32) - num_specs
    thd_offset_lp, thd_offset_str = __1Dcreator('thd_offset', offsets)

    #kernel data
    kernel_data = [T_arr, P_arr, concs_lp, thd_lp, thd_type_lp,
                   thd_eff_lp, thd_spec_lp, thd_num_spec_lp, thd_offset_lp,
                   thd_eff_ns_lp]

    #maps
    out_map = {}
    outmap_name = 'out_map'
    indicies = rate_info['thd']['map'].astype(dtype=np.int32)
    indicies = __handle_indicies(indicies, reac_ind, out_map, kernel_data)
    #extra loops
    extra_inames = [('k', '0 <= k < num')]

    #generate instructions
    instructions = Template("""
<> offset = ${offset}
<> num_temp = ${num_spec_str} {id=num0}
if ${type_str} == 1 # single species
    <> thd_temp = ${conc_spec} {id=thd0, dep=num0}
    num_temp = 0 {id=num1, dep=num0}
else
    thd_temp = P_arr[j] * ${thd_eff_ns_str} / (R * T_arr[j]) {id=thd1, dep=num0}
end
<> num = num_temp {dep=num*}
for k
    thd_temp = thd_temp + ${thd_eff} * ${conc_thd_spec} {id=thdcalc}
end
${thd_str} = thd_temp {dep=thd*}
""")

    #sub in instructions
    instructions = Template(
        instructions.safe_substitute(
            offset=thd_offset_str,
            type_str=thd_type_str,
            conc_spec=concs_str.safe_substitute(
                    species_ind=thd_spec_str),
            num_spec_str=thd_num_spec_str,
            thd_eff=Template(thd_eff_str).safe_substitute(reac_ind='offset + k'),
            conc_thd_spec=concs_str.safe_substitute(
                species_ind=Template(thd_spec_str).safe_substitute(
                    reac_ind='offset + k')),
            thd_str=thd_str,
            thd_eff_ns_str=thd_eff_ns_str
        )
    ).safe_substitute(reac_ind=reac_ind)


    #create info
    info = rateconst_info('thd',
                         instructions=instructions,
                         reac_ind=reac_ind,
                         kernel_data=kernel_data,
                         extra_inames=extra_inames,
                         indicies=indicies,
                         parameters={'R' : chem.RU})
    return info

def get_cheb_arrhenius_rates(eqs, loopy_opt, rate_info, test_size=None):
    """Generates instructions, kernel arguements, and data for cheb rate constants

    Parameters
    ----------
    eqs : dict
        Sympy equations / variables for constant pressure / constant volume systems
    loopy_opt : `loopy_options` object
        A object containing all the loopy options to execute
    rate_info : dict
        The output of :method:`assign_rates` for this mechanism
    test_size : int
        If not none, this kernel is being used for testing.
        Hence we need to size the arrays accordingly

    Returns
    -------
    rate_list : list of :class:`rateconst_info`
        The generated infos for feeding into the kernel generator

    """

    #the equation set doesn't matter for this application
    #just use conp
    conp_eqs = eqs['conp']

    rate_eqn_pre = get_rate_eqn(eqs)
    #find the cheb equation
    cheb_eqn = next(x for x in conp_eqs if str(x) == 'log({k_f}[i])/log(10)')
    cheb_form, cheb_eqn = cheb_eqn, conp_eqs[cheb_eqn][(reaction_type.cheb,)]
    cheb_form = sp.Pow(10, sp.Symbol('kf_temp'))

    #make nice symbols
    Tinv = sp.Symbol('T_inv')
    logP = sp.Symbol('logP')
    Pmax, Pmin, Tmax, Tmin = sp.symbols('Pmax Pmin Tmax Tmin')
    Pred, Tred = sp.symbols('Pred Tred')

    #get tilde{T}, tilde{P}
    T_red = next(x for x in conp_eqs if str(x) == 'tilde{T}')
    P_red = next(x for x in conp_eqs if str(x) == 'tilde{P}')

    Pred_eqn = sp_utils.sanitize(conp_eqs[P_red], subs={sp.log(sp.Symbol('P_{min}')) : Pmin,
                                               sp.log(sp.Symbol('P_{max}')) : Pmax,
                                               sp.log(sp.Symbol('P')) : logP})

    Tred_eqn = sp_utils.sanitize(conp_eqs[T_red], subs={sp.S.One / sp.Symbol('T_{min}') : Tmin,
                                               sp.S.One / sp.Symbol('T_{max}') : Tmax,
                                               sp.S.One / sp.Symbol('T') : Tinv})

    #number of cheb reactions
    num_cheb = rate_info['cheb']['num']
    # degree of pressure polynomial per reaction
    num_P = np.array(rate_info['cheb']['num_P'], dtype=np.int32)
    # degree of temperature polynomial per reaction
    num_T = np.array(rate_info['cheb']['num_T'], dtype=np.int32)

    #max degrees in mechanism
    maxP = int(np.max(num_P))
    maxT = int(np.max(num_T))
    minP = int(np.min(num_P))
    minT = int(np.min(num_T))
    poly_max = int(np.maximum(maxP, maxT))

    #now we start defining parameters / temporary variable

    #workspace vars
    pres_poly_lp = lp.TemporaryVariable('pres_poly', shape=(poly_max,),
        dtype=np.float64, scope=scopes.PRIVATE, read_only=False)
    temp_poly_lp = lp.TemporaryVariable('temp_poly', shape=(poly_max,),
        dtype=np.float64, scope=scopes.PRIVATE, read_only=False)
    pres_poly_str = Template('pres_poly[${pres_poly_ind}]')
    temp_poly_str = Template('temp_poly[${temp_poly_ind}]')

    #chebyshev parameters
    params = np.zeros((num_cheb, maxT, maxP))
    for i, p in enumerate(rate_info['cheb']['params']):
        params[i, :num_T[i], :num_P[i]] = p[:, :]

    indicies=['${reac_ind}', '${temp_poly_ind}', '${pres_poly_ind}']
    params_lp = lp.TemporaryVariable('cheb_params', shape=params.shape,
        initializer=params, scope=scopes.GLOBAL, read_only=True)
    params_str = Template('cheb_params[' + ','.join(indicies) +  ']')

    #finally the min/maxs & param #'s
    numP_lp = lp.TemporaryVariable('cheb_numP', shape=num_P.shape,
        initializer=num_P, dtype=np.int32, read_only=True, scope=scopes.GLOBAL)
    numP_str = 'cheb_numP[${reac_ind}]'
    numT_lp = lp.TemporaryVariable('cheb_numT', shape=num_T.shape,
        initializer=num_T, dtype=np.int32, read_only=True, scope=scopes.GLOBAL)
    numT_str = 'cheb_numT[${reac_ind}]'

    # limits for cheby polys
    Plim = np.log(np.array(rate_info['cheb']['Plim'], dtype=np.float64))
    Tlim = 1. / np.array(rate_info['cheb']['Tlim'], dtype=np.float64)

    indicies = ['${reac_ind}', '${lim_ind}']
    plim_lp = lp.TemporaryVariable('cheb_plim', shape=Plim.shape,
        initializer=Plim, scope=scopes.GLOBAL, read_only=True)
    tlim_lp = lp.TemporaryVariable('cheb_tlim', shape=Tlim.shape,
        initializer=Tlim, scope=scopes.GLOBAL, read_only=True)
    plim_str = Template('cheb_plim[' + ','.join(indicies) + ']')
    tlim_str = Template('cheb_tlim[' + ','.join(indicies) + ']')

    T_arr, T_arr_str, _ = lp_utils.get_loopy_arg('T_arr',
                          indicies=['j'],
                          dimensions=[test_size],
                          order=loopy_opt.order,
                          dtype=np.float64)
    P_arr, P_arr_str, _= lp_utils.get_loopy_arg('P_arr',
                          indicies=['j'],
                          dimensions=[test_size],
                          order=loopy_opt.order,
                          dtype=np.float64)
    kernel_data = [params_lp, numP_lp, numT_lp, plim_lp, tlim_lp,
                    pres_poly_lp, temp_poly_lp, T_arr, P_arr]

    reac_ind = 'i'
    out_map = {}
    outmap_name = 'out_map'
    indicies = rate_info['cheb']['map'].astype(dtype=np.int32)
    Nr = rate_info['Nr']

    indicies = __handle_indicies(indicies, reac_ind,
                      out_map, kernel_data, outmap_name=outmap_name)

    #get the proper kf indexing / array
    kf_arr, kf_str, map_result = lp_utils.get_loopy_arg('kf',
                    indicies=[reac_ind, 'j'],
                    dimensions=[Nr, test_size],
                    order=loopy_opt.order,
                    map_name=out_map)

    maps = [map_result[reac_ind]]

    #add to kernel data
    kernel_data.append(kf_arr)

    #preinstructions
    preinstructs = [__PLOG_PREINST_KEY, __TINV_PREINST_KEY]

    #extra loops
    pres_poly_ind = 'k'
    temp_poly_ind = 'm'
    extra_inames = [(pres_poly_ind, '0 <= {} < {}'.format(pres_poly_ind, maxP)),
                    (temp_poly_ind, '0 <= {} < {}'.format(temp_poly_ind, maxT)),
                    ('p', '2 <= p < {}'.format(poly_max))]

    instructions = Template("""
<>Pmin = ${Pmin_str}
<>Tmin = ${Tmin_str}
<>Pmax = ${Pmax_str}
<>Tmax = ${Tmax_str}
<>Tred = ${Tred_str}
<>Pred = ${Pred_str}
<>numP = ${numP_str} {id=plim}
<>numT = ${numT_str} {id=tlim}
${ppoly_0} = 1
${ppoly_1} = Pred
${tpoly_0} = 1
${tpoly_1} = Tred
#<> poly_end = max(numP, numT)
#compute polynomial terms
for p
    if p < numP
        ${ppoly_p} = 2 * Pred * ${ppoly_pm1} - ${ppoly_pm2} {id=ppoly, dep=plim}
    end
    if p < numT
        ${tpoly_p} = 2 * Tred * ${tpoly_pm1} - ${tpoly_pm2} {id=tpoly, dep=tlim}
    end
end
<> kf_temp = 0
for m
    <>temp = 0
    for k
        temp = temp + ${ppoly_k} * ${chebpar_km} {id=temp, dep=ppoly:tpoly}
    end
    kf_temp = kf_temp + ${tpoly_m} * temp {id=kf, dep=temp}
end

${kf_str} = exp10(kf_temp) {dep=kf}
""")

    instructions = Template(instructions.safe_substitute(
                    kf_str=kf_str,
                    Tred_str=str(Tred_eqn),
                    Pred_str=str(Pred_eqn),
                    Pmin_str=plim_str.safe_substitute(lim_ind=0),
                    Pmax_str=plim_str.safe_substitute(lim_ind=1),
                    Tmin_str=tlim_str.safe_substitute(lim_ind=0),
                    Tmax_str=tlim_str.safe_substitute(lim_ind=1),
                    ppoly_0=pres_poly_str.safe_substitute(pres_poly_ind=0),
                    ppoly_1=pres_poly_str.safe_substitute(pres_poly_ind=1),
                    ppoly_k=pres_poly_str.safe_substitute(pres_poly_ind=pres_poly_ind),
                    ppoly_p=pres_poly_str.safe_substitute(pres_poly_ind='p'),
                    ppoly_pm1=pres_poly_str.safe_substitute(pres_poly_ind='p - 1'),
                    ppoly_pm2=pres_poly_str.safe_substitute(pres_poly_ind='p - 2'),
                    tpoly_0=temp_poly_str.safe_substitute(temp_poly_ind=0),
                    tpoly_1=temp_poly_str.safe_substitute(temp_poly_ind=1),
                    tpoly_m=temp_poly_str.safe_substitute(temp_poly_ind=temp_poly_ind),
                    tpoly_p=temp_poly_str.safe_substitute(temp_poly_ind='p'),
                    tpoly_pm1=temp_poly_str.safe_substitute(temp_poly_ind='p - 1'),
                    tpoly_pm2=temp_poly_str.safe_substitute(temp_poly_ind='p - 2'),
                    chebpar_km=params_str.safe_substitute(temp_poly_ind=temp_poly_ind,
                        pres_poly_ind=pres_poly_ind),
                    numP_str=numP_str,
                    numT_str=numT_str,
                    kf_eval=str(cheb_form),
                    num_cheb=num_cheb)).safe_substitute(reac_ind=reac_ind)

    return rateconst_info('cheb', instructions=instructions, pre_instructions=preinstructs,
                     reac_ind=reac_ind, kernel_data=kernel_data, maps=maps,
                     extra_inames=extra_inames, indicies=indicies)


def get_plog_arrhenius_rates(eqs, loopy_opt, rate_info, test_size=None):
    """Generates instructions, kernel arguements, and data for p-log rate constants

    Parameters
    ----------
    eqs : dict
        Sympy equations / variables for constant pressure / constant volume systems
    loopy_opt : `loopy_options` object
        A object containing all the loopy options to execute
    rate_info : dict
        The output of :method:`assign_rates` for this mechanism
    test_size : int
        If not none, this kernel is being used for testing.
        Hence we need to size the arrays accordingly

    Returns
    -------
    rate_list : list of :class:`rateconst_info`
        The generated infos for feeding into the kernel generator

    """

    rate_eqn = get_rate_eqn(eqs)

    #find the plog equation
    plog_eqn = next(x for x in eqs['conp'] if str(x) == 'log({k_f}[i])')
    plog_form, plog_eqn = plog_eqn, eqs['conp'][plog_eqn][(reaction_type.plog,)]

    #now we do some surgery to obtain a form w/o 'logs' as we'll take them
    #explicitly in python
    logP = sp.Symbol('logP')
    logP1 = sp.Symbol('low[0]')
    logP2 = sp.Symbol('hi[0]')
    logk1 = sp.Symbol('logk1')
    logk2 = sp.Symbol('logk2')
    plog_eqn = sp_utils.sanitize(plog_eqn, subs={sp.log(sp.Symbol('k_1')) : logk1,
                                        sp.log(sp.Symbol('k_2')) : logk2,
                                        sp.log(sp.Symbol('P')) : logP,
                                        sp.log(sp.Symbol('P_1')) : logP1,
                                        sp.log(sp.Symbol('P_2')) : logP2})

    #and specialize the k1 / k2 equations
    A1 = sp.Symbol('low[1]')
    b1 = sp.Symbol('low[2]')
    Ta1 = sp.Symbol('low[3]')
    k1_eq = sp_utils.sanitize(rate_eqn, subs={sp.Symbol('A[i]') : A1,
                                         sp.Symbol('beta[i]') : b1,
                                         sp.Symbol('Ta[i]') : Ta1})
    A2 = sp.Symbol('hi[1]')
    b2 = sp.Symbol('hi[2]')
    Ta2 = sp.Symbol('hi[3]')
    k2_eq = sp_utils.sanitize(rate_eqn, subs={sp.Symbol('A[i]') : A2,
                                         sp.Symbol('beta[i]') : b2,
                                         sp.Symbol('Ta[i]') : Ta2})

    # total # of plog reactions
    num_plog = rate_info['plog']['num']
    # number of parameter sets per reaction
    num_params = rate_info['plog']['num_P']

    #create the loopy equivalents
    params = rate_info['plog']['params']
    #create a copy
    params_temp = [p[:] for p in params]
    #max # of parameters for sizing
    maxP = np.max(num_params)

    #for simplicity, we're going to use a padded form
    params = np.zeros((4, num_plog, maxP))
    for m in range(4):
        for i, numP in enumerate(num_params):
            for j in range(numP):
                params[m, i, j] = params_temp[i][j][m]

    #take the log of P and A
    hold = np.seterr(divide='ignore')
    params[0, :, :] = np.log(params[0, :, :])
    params[1, :, :] = np.log(params[1, :, :])
    params[np.where(np.isinf(params))] = 0
    np.seterr(**hold)

    #default params indexing order
    inds = ['${m}', '${reac_ind}', '${param_ind}']
    #make loopy version
    plog_params_lp = lp.TemporaryVariable('plog_params', shape=params.shape,
        initializer=params, scope=scopes.GLOBAL, read_only=True)
    param_str = Template('plog_params[' + ','.join(inds) + ']')

    #and finally the loopy version of num_params
    num_params_lp = lp.TemporaryVariable('plog_num_params', shape=lp.auto,
        initializer=num_params, read_only=True, scope=scopes.GLOBAL)

    #create temporary variables
    low_lp = lp.TemporaryVariable('low', shape=(4,), scope=scopes.PRIVATE, dtype=np.float64)
    hi_lp = lp.TemporaryVariable('hi', shape=(4,), scope=scopes.PRIVATE, dtype=np.float64)
    T_arr = lp.GlobalArg('T_arr', shape=(test_size,), dtype=np.float64)
    P_arr = lp.GlobalArg('P_arr', shape=(test_size,), dtype=np.float64)

    #start creating the rateconst_info's
    #data
    kernel_data = [plog_params_lp, num_params_lp, T_arr,
                        P_arr, low_lp, hi_lp]

    #reac ind
    reac_ind = 'i'

    #extra loops
    extra_inames = [('k', '0 <= k < {}'.format(maxP - 1)), ('m', '0 <= m < 4')]

    #see if we need an output mask
    out_map = {}
    indicies = rate_info['plog']['map'].astype(dtype=np.int32)
    Nr = rate_info['Nr']

    indicies = __handle_indicies(indicies, reac_ind, out_map,
                    kernel_data, outmap_name='plog_inds')

    #get the proper kf indexing / array
    kf_arr, kf_str, map_result = lp_utils.get_loopy_arg('kf',
                    indicies=[reac_ind, 'j'],
                    dimensions=[Nr, test_size],
                    map_name=out_map,
                    order=loopy_opt.order)
    kernel_data.append(kf_arr)

    #handle map info
    maps = []
    if reac_ind in map_result:
        maps.append(map_result[reac_ind])

    #instructions
    instructions = Template(Template(
"""
        <>lower = logP <= ${pressure_lo} #check below range
        if lower
            <>lo_ind = 0 {id=ind00}
            <>hi_ind = 0 {id=ind01}
        end
        <>numP = plog_num_params[${reac_ind}] - 1
        <>upper = logP > ${pressure_hi} #check above range
        if upper
            lo_ind = numP {id=ind10}
            hi_ind = numP {id=ind11}
        end
        <>oor = lower or upper
        for k
            #check that
            #1. inside this reactions parameter's still
            #2. inside pressure range
            <> midcheck = (k <= numP) and (logP > ${pressure_mid_lo}) and (logP <= ${pressure_mid_hi})
            if midcheck
                lo_ind = k {id=ind20}
                hi_ind = k + 1 {id=ind21}
            end
        end
        for m
            low[m] = ${pressure_general_lo} {id=lo, dep=ind*}
            hi[m] = ${pressure_general_hi} {id=hi, dep=ind*}
        end
        <>logk1 = ${loweq} {id=a1, dep=lo}
        <>logk2 = ${hieq} {id=a2, dep=hi}
        <>kf_temp = logk1 {id=a_oor}
        if not oor
            kf_temp = ${plog_eqn} {id=a_found, dep=a1:a2}
        end
        ${kf_str} = exp(kf_temp) {id=kf, dep=aoor:a_found}
""").safe_substitute(loweq=k1_eq, hieq=k2_eq, plog_eqn=plog_eqn,
                    kf_str=kf_str,
                    pressure_lo=param_str.safe_substitute(m=0, param_ind=0),
                    pressure_hi=param_str.safe_substitute(m=0, param_ind='numP'),
                    pressure_mid_lo=param_str.safe_substitute(m=0, param_ind='k'),
                    pressure_mid_hi=param_str.safe_substitute(m=0, param_ind='k + 1'),
                    pressure_general_lo=param_str.safe_substitute(m='m', param_ind='lo_ind'),
                    pressure_general_hi=param_str.safe_substitute(m='m', param_ind='hi_ind')
                    )
    ).safe_substitute(reac_ind=reac_ind)

    #and return
    return [rateconst_info(name='plog', instructions=instructions,
        pre_instructions=[__TINV_PREINST_KEY, __TLOG_PREINST_KEY, __PLOG_PREINST_KEY],
        reac_ind=reac_ind, kernel_data=kernel_data,
        maps=maps, extra_inames=extra_inames, indicies=indicies)]


def get_reduced_pressure_kernel(eqs, loopy_opt, rate_info, test_size=None):
    """Generates instructions, kernel arguements, and data for the reduced
    pressure evaluation kernel

    Parameters
    ----------
    eqs : dict
        Sympy equations / variables for constant pressure / constant volume systems
    loopy_opt : `loopy_options` object
        A object containing all the loopy options to execute
    rate_info : dict
        The output of :method:`assign_rates` for this mechanism
    test_size : int
        If not none, this kernel is being used for testing.
        Hence we need to size the arrays accordingly

    Returns
    -------
    rate_list : list of :class:`rateconst_info`
        The generated infos for feeding into the kernel generator

    """

    reac_ind = 'i'
    conp_eqs = eqs['conp'] #conp / conv irrelevant for rates

    #create the various necessary arrays

    kernel_data = []

    map_instructs = []
    inmaps = {}

    #falloff index mapping
    indicies = ['${reac_ind}', 'j']
    __handle_indicies(rate_info['fall']['map'], '${reac_ind}', inmaps, kernel_data,
                            outmap_name='fall_map',
                            force_zero=True)

    #simple arrhenius rates
    kf_arr, kf_str, map_inst = lp_utils.get_loopy_arg('kf',
                    indicies=indicies,
                    dimensions=[rate_info['Nr'], test_size],
                    order=loopy_opt.order,
                    map_name=inmaps)
    if '${reac_ind}' in map_inst:
        map_instructs.append(map_inst['${reac_ind}'])

    #simple arrhenius rates using falloff (alternate) parameters
    kf_fall_arr, kf_fall_str, _ = lp_utils.get_loopy_arg('kf_fall',
                        indicies=indicies,
                        dimensions=[rate_info['fall']['num'], test_size],
                        order=loopy_opt.order)

    #temperatures
    T_arr = lp.GlobalArg('T_arr', shape=(test_size,), dtype=np.float64)

    #create a Pr array
    Pr_arr, Pr_str, _ = lp_utils.get_loopy_arg('Pr',
                        indicies=indicies,
                        dimensions=[rate_info['fall']['num'], test_size],
                        order=loopy_opt.order)

    #third-body concentrations
    thd_indexing_str = '${reac_ind}'
    thd_index_map = {}

    #check if a mapping is needed
    thd_inds = rate_info['thd']['map']
    fall_inds = rate_info['fall']['map']
    if not np.array_equal(thd_inds, fall_inds):
        #add a mapping for falloff index -> third body conc index
        thd_map_name = 'thd_map'
        thd_map = np.where(np.in1d(thd_inds, fall_inds))[0].astype(np.int32)

        __handle_indicies(thd_map, thd_indexing_str, thd_index_map,
            kernel_data, outmap_name=thd_map_name, force_map=True)

    #create the array
    indicies = ['${reac_ind}', 'j']
    thd_conc_lp, thd_conc_str, map_inst = lp_utils.get_loopy_arg('thd_conc',
                                                 indicies=indicies,
                                                 dimensions=(rate_info['thd']['num'], test_size),
                                                 order=loopy_opt.order,
                                                 map_name=thd_index_map,
                                                 map_result='thd_i')
    if '${reac_ind}' in map_inst:
        map_instructs.append(map_inst['${reac_ind}'])

    #and finally the falloff types
    fall_type_lp, fall_type_str = __1Dcreator('fall_type', rate_info['fall']['ftype'],
        scope=scopes.GLOBAL)

    #append all arrays to the kernel data
    kernel_data.extend([T_arr, thd_conc_lp, kf_arr, kf_fall_arr, Pr_arr,
        fall_type_lp])

    #create Pri eqn
    Pri_sym = next(x for x in conp_eqs if str(x) == 'P_{r, i}')
    #make substituions to get a usable form
    pres_mod_sym = sp.Symbol(thd_conc_str)
    Pri_eqn = sp_utils.sanitize(conp_eqs[Pri_sym][(thd_body_type.mix,)],
                       subs={'[X]_i' : pres_mod_sym,
                            'k_{0, i}' : 'k0',
                            'k_{infty, i}' : 'kinf'}
                      )

    #create instruction set
    pr_instructions = Template("""
if fall_type[${reac_ind}]
    #chemically activated
    <>k0 = ${kf_str} {id=k0_c}
    <>kinf = ${kf_fall_str} {id=kinf_c}
else
    #fall-off
    kinf = ${kf_str} {id=kinf_f}
    k0 = ${kf_fall_str} {id=k0_f}
end
${Pr_str} = ${Pr_eq} {dep=k*}
""")

    #sub in strings
    pr_instructions = Template(pr_instructions.safe_substitute(
    kf_str=kf_str,
    kf_fall_str=kf_fall_str,
    Pr_str=Pr_str,
    Pr_eq=Pri_eqn)).safe_substitute(
        reac_ind=reac_ind)

    #and finally return the resulting info
    return [rateconst_info('red_pres',
                     instructions=pr_instructions,
                     reac_ind=reac_ind,
                     kernel_data=kernel_data,
                     indicies=np.array(range(rate_info['fall']['num']), dtype=np.int32),
                     maps=map_instructs)]

def get_troe_kernel(eqs, loopy_opt, rate_info, test_size=None):
    """Generates instructions, kernel arguements, and data for the Troe
    falloff evaluation kernel

    Parameters
    ----------
    eqs : dict
        Sympy equations / variables for constant pressure / constant volume systems
    loopy_opt : `loopy_options` object
        A object containing all the loopy options to execute
    rate_info : dict
        The output of :method:`assign_rates` for this mechanism
    test_size : int
        If not none, this kernel is being used for testing.
        Hence we need to size the arrays accordingly

    Returns
    -------
    rate_list : list of :class:`rateconst_info`
        The generated infos for feeding into the kernel generator

    """

    #set of equations is irrelevant for non-derivatives
    conp_eqs = eqs['conp']

    #rate info and reac ind
    reac_ind = 'i'
    kernel_data = []

    #add the troe map
    #add sri map
    out_map = {}
    outmap_name = 'out_map'
    indicies = np.array(rate_info['fall']['troe']['map'], dtype=np.int32)
    indicies = __handle_indicies(indicies, '${reac_ind}', out_map, kernel_data,
                                force_zero=True)

    #create the Pr loopy array / string
    Pr_lp, Pr_str, map_result = lp_utils.get_loopy_arg('Pr',
                            indicies=['${reac_ind}', 'j'],
                            dimensions=[rate_info['fall']['num'], test_size],
                            order=loopy_opt.order,
                            map_name=out_map)

    #create Fi loopy array / string
    Fi_lp, Fi_str, map_result = lp_utils.get_loopy_arg('Fi',
                        indicies=['${reac_ind}', 'j'],
                        dimensions=[rate_info['fall']['num'], test_size],
                        order=loopy_opt.order,
                        map_name=out_map)

    #add to the kernel maps
    maps = []
    if '${reac_ind}' in map_result:
        maps.append(map_result['${reac_ind}'])

    #create the Fcent loopy array / str
    Fcent_lp, Fcent_str, _ = lp_utils.get_loopy_arg('Fcent',
                        indicies=['${reac_ind}', 'j'],
                        dimensions=[rate_info['fall']['troe']['num'], test_size],
                        order=loopy_opt.order)

    #create the Atroe loopy array / str
    Atroe_lp, Atroe_str, _ = lp_utils.get_loopy_arg('Atroe',
                        indicies=['${reac_ind}', 'j'],
                        dimensions=[rate_info['fall']['troe']['num'], test_size],
                        order=loopy_opt.order)

    #create the Fcent loopy array / str
    Btroe_lp, Btroe_str, _ = lp_utils.get_loopy_arg('Btroe',
                        indicies=['${reac_ind}', 'j'],
                        dimensions=[rate_info['fall']['troe']['num'], test_size],
                        order=loopy_opt.order)

    #create the temperature array
    T_lp = lp.GlobalArg('T_arr', shape=(test_size,), dtype=np.float64)

    #update the kernel_data
    kernel_data.extend([Pr_lp, T_lp, Fi_lp, Fcent_lp, Atroe_lp, Btroe_lp])

    #find the falloff form equations
    Fi_sym = next(x for x in conp_eqs if str(x) == 'F_{i}')
    keys = conp_eqs[Fi_sym]
    Fi = {}
    for key in keys:
        fall_form = next(x for x in key if isinstance(x, falloff_form))
        Fi[fall_form] = conp_eqs[Fi_sym][key]

    #get troe syms / eqs
    Fcent = next(x for x in Fi[falloff_form.troe].free_symbols if str(x) == 'F_{cent}')
    Atroe = next(x for x in Fi[falloff_form.troe].free_symbols if str(x) == 'A_{Troe}')
    Btroe = next(x for x in Fi[falloff_form.troe].free_symbols if str(x) == 'B_{Troe}')
    Fcent_eq, Atroe_eq, Btroe_eq = conp_eqs[Fcent], conp_eqs[Atroe], conp_eqs[Btroe]

    #get troe params and create arrays
    troe_a, troe_T3, troe_T1, troe_T2 = rate_info['fall']['troe']['a'], \
         rate_info['fall']['troe']['T3'], \
         rate_info['fall']['troe']['T1'], \
         rate_info['fall']['troe']['T2']
    troe_a_lp, troe_a_str = __1Dcreator('troe_a', troe_a, scope=scopes.GLOBAL)
    troe_T3_lp, troe_T3_str = __1Dcreator('troe_T3', troe_T3, scope=scopes.GLOBAL)
    troe_T1_lp, troe_T1_str = __1Dcreator('troe_T1', troe_T1, scope=scopes.GLOBAL)
    troe_T2_lp, troe_T2_str = __1Dcreator('troe_T2', troe_T2, scope=scopes.GLOBAL)
    #update the kernel_data
    kernel_data.extend([troe_a_lp, troe_T3_lp, troe_T1_lp, troe_T2_lp])
    #sub into eqs
    Fcent_eq = sp_utils.sanitize(Fcent_eq, subs={
            'a' : troe_a_str,
            'T^{*}' : troe_T1_str,
            'T^{***}' : troe_T3_str,
            'T^{**}' : troe_T2_str,
        })

    #now separate into optional / base parts
    Fcent_base_eq = sp.Add(*[x for x in sp.Add.make_args(Fcent_eq) if not sp.Symbol(troe_T2_str) in x.free_symbols])
    Fcent_opt_eq = Fcent_eq - Fcent_base_eq

    #develop the Atroe / Btroe eqs
    Atroe_eq = sp_utils.sanitize(Atroe_eq, subs=OrderedDict([
            ('F_{cent}', Fcent_str),
            ('P_{r, i}', Pr_str),
            (sp.log(sp.Symbol(Pr_str), 10), 'logPr'),
            (sp.log(sp.Symbol(Fcent_str), 10), sp.Symbol('logFcent'))
        ]))

    Btroe_eq = sp_utils.sanitize(Btroe_eq, subs=OrderedDict([
            ('F_{cent}', Fcent_str),
            ('P_{r, i}', Pr_str),
            (sp.log(sp.Symbol(Pr_str), 10), 'logPr'),
            (sp.log(sp.Symbol(Fcent_str), 10), sp.Symbol('logFcent'))
        ]))

    Fcent_temp_str = 'Fcent_temp'
    #finally, work on the Fi form
    Fi_eq = sp_utils.sanitize(Fi[falloff_form.troe], subs=OrderedDict([
            ('F_{cent}', Fcent_temp_str),
            ('A_{Troe}', Atroe_str),
            ('B_{Troe}', Btroe_str)
        ]))

    #separate into Fcent and power
    Fi_base_eq = next(x for x in Fi_eq.args if str(x) == Fcent_temp_str)
    Fi_pow_eq = next(x for x in Fi_eq.args if str(x) != Fcent_temp_str)
    Fi_pow_eq = sp_utils.sanitize(Fi_pow_eq, subs=OrderedDict([
            (sp.Pow(sp.Symbol(Atroe_str), 2), sp.Symbol('Atroe_squared')),
            (sp.Pow(sp.Symbol(Btroe_str), 2), sp.Symbol('Btroe_squared'))
        ]))

    #make the instructions
    troe_instructions = Template(
    """
    <>T = T_arr[j]
    <>${Fcent_temp} = ${Fcent_base_eq} {id=Fcent_decl} #this must be a temporary to avoid a race on future assignments
    if ${troe_T2_str} != 0
        ${Fcent_temp} = ${Fcent_temp} + ${Fcent_opt_eq} {id=Fcent_decl2, dep=Fcent_decl}
    end
    ${Fcent_str} = ${Fcent_temp} {dep=Fcent_decl*}
    <>logFcent = log10(${Fcent_temp}) {dep=Fcent_decl*}
    <>logPr = log10(${Pr_str}) {id=Pr_decl}
    <>Atroe_temp = ${Atroe_eq} {dep=Fcent_decl*:Pr_decl}
    <>Btroe_temp = ${Btroe_eq} {dep=Fcent_decl*:Pr_decl}
    ${Atroe_str} = Atroe_temp #this must be a temporary to avoid a race on future assignments
    ${Btroe_str} = Btroe_temp #this must be a temporary to avoid a race on future assignments
    <>Atroe_squared = Atroe_temp * Atroe_temp
    <>Btroe_squared = Btroe_temp * Btroe_temp
    ${Fi_str} = ${Fi_base_eq}**(${Fi_pow_eq}) {dep=Fcent_decl*:Pr_decl}
    """
    ).safe_substitute(Fcent_temp=Fcent_temp_str,
                     Fcent_str=Fcent_str,
                     Fcent_base_eq=Fcent_base_eq,
                     Fcent_opt_eq=Fcent_opt_eq,
                     troe_T2_str=troe_T2_str,
                     Pr_str=Pr_str,
                     Atroe_eq=Atroe_eq,
                     Btroe_eq=Btroe_eq,
                     Atroe_str=Atroe_str,
                     Btroe_str=Btroe_str,
                     Fi_str=Fi_str,
                     Fi_base_eq=Fi_base_eq,
                     Fi_pow_eq=Fi_pow_eq)
    troe_instructions = Template(troe_instructions).safe_substitute(reac_ind=reac_ind)

    return [rateconst_info('fall_troe',
                     instructions=troe_instructions,
                     reac_ind=reac_ind,
                     kernel_data=kernel_data,
                     indicies=indicies,
                     maps=maps)]


def get_sri_kernel(eqs, loopy_opt, rate_info, test_size=None):
    """Generates instructions, kernel arguements, and data for the SRI
    falloff evaluation kernel

    Parameters
    ----------
    eqs : dict
        Sympy equations / variables for constant pressure / constant volume systems
    loopy_opt : `loopy_options` object
        A object containing all the loopy options to execute
    rate_info : dict
        The output of :method:`assign_rates` for this mechanism
    test_size : int
        If not none, this kernel is being used for testing.
        Hence we need to size the arrays accordingly

    Returns
    -------
    rate_list : list of :class:`rateconst_info`
        The generated infos for feeding into the kernel generator

    """

    #set of equations is irrelevant for non-derivatives
    conp_eqs = eqs['conp']
    reac_ind = 'i'

    #Create the temperature array
    T_arr = lp.GlobalArg('T_arr', shape=(test_size,), dtype=np.float64)
    kernel_data = [T_arr]

    #figure out if we need to do any mapping of the input variable
    out_map = {}
    outmap_name = 'out_map'
    indicies = np.array(rate_info['fall']['sri']['map'], dtype=np.int32)
    indicies = __handle_indicies(indicies, '${reac_ind}', out_map, kernel_data,
                                force_zero=True)

    #start creating SRI kernel
    Fi_sym = next(x for x in conp_eqs if str(x) == 'F_{i}')
    keys = conp_eqs[Fi_sym]
    Fi = {}
    for key in keys:
        fall_form = next(x for x in key if isinstance(x, falloff_form))
        Fi[fall_form] = conp_eqs[Fi_sym][key]

    #find Pr symbol
    Pri_sym = next(x for x in conp_eqs if str(x) == 'P_{r, i}')

    #create Fi array / mapping
    Fi_lp, Fi_str, map_result = lp_utils.get_loopy_arg('Fi',
                        indicies=['${reac_ind}', 'j'],
                        dimensions=[rate_info['fall']['num'], test_size],
                        order=loopy_opt.order,
                        map_name=out_map)
    #and Pri array / mapping
    Pr_lp, Pr_str, map_result = lp_utils.get_loopy_arg('Pr',
                        indicies=['${reac_ind}', 'j'],
                        dimensions=[rate_info['fall']['num'], test_size],
                        order=loopy_opt.order,
                        map_name=out_map)
    maps = []
    if '${reac_ind}' in map_result:
        maps.append(map_result['${reac_ind}'])
    kernel_data.extend([Fi_lp, Pr_lp])

    #get SRI symbols
    X_sri_sym = next(x for x in Fi[falloff_form.sri].free_symbols if str(x) == 'X')
    a_sri_sym = next(x for x in Fi[falloff_form.sri].free_symbols if str(x) == 'a')
    b_sri_sym = next(x for x in Fi[falloff_form.sri].free_symbols if str(x) == 'b')
    c_sri_sym = next(x for x in Fi[falloff_form.sri].free_symbols if str(x) == 'c')
    d_sri_sym = next(x for x in Fi[falloff_form.sri].free_symbols if str(x) == 'd')
    e_sri_sym = next(x for x in Fi[falloff_form.sri].free_symbols if str(x) == 'e')
    #get SRI params and create arrays
    sri_a, sri_b, sri_c, sri_d, sri_e = rate_info['fall']['sri']['a'], \
        rate_info['fall']['sri']['b'], \
        rate_info['fall']['sri']['c'], \
        rate_info['fall']['sri']['d'], \
        rate_info['fall']['sri']['e']
    X_sri_lp, X_sri_str, _ = lp_utils.get_loopy_arg('X',
                        indicies=['${reac_ind}', 'j'],
                        dimensions=[rate_info['fall']['sri']['num'], test_size],
                        order=loopy_opt.order)
    sri_a_lp, sri_a_str = __1Dcreator('sri_a', sri_a, scope=scopes.GLOBAL)
    sri_b_lp, sri_b_str = __1Dcreator('sri_b', sri_b, scope=scopes.GLOBAL)
    sri_c_lp, sri_c_str = __1Dcreator('sri_c', sri_c, scope=scopes.GLOBAL)
    sri_d_lp, sri_d_str = __1Dcreator('sri_d', sri_d, scope=scopes.GLOBAL)
    sri_e_lp, sri_e_str = __1Dcreator('sri_e', sri_e, scope=scopes.GLOBAL)
    kernel_data.extend([X_sri_lp, sri_a_lp, sri_b_lp, sri_c_lp, sri_d_lp, sri_e_lp])

    #create SRI eqs
    X_sri_eq = conp_eqs[X_sri_sym].subs(sp.Pow(sp.log(Pri_sym, 10), 2), 'logPr * logPr')
    Fi_sri_eq = Fi[falloff_form.sri]
    Fi_sri_eq = sp_utils.sanitize(Fi_sri_eq,
        subs={
            'a' : sri_a_str,
            'b' : sri_b_str,
            'c' : sri_c_str,
            'd' : sri_d_str,
            'e' : sri_e_str,
            'X' : 'X_temp'
        })
    #do some surgery on the Fi_sri_eq to get the optional parts
    Fi_sri_base = next(x for x in sp.Mul.make_args(Fi_sri_eq)
                       if any(str(y) == sri_a_str for y in x.free_symbols))
    Fi_sri_opt = Fi_sri_eq / Fi_sri_base
    Fi_sri_d_opt = next(x for x in sp.Mul.make_args(Fi_sri_opt)
                       if any(str(y) == sri_d_str for y in x.free_symbols))
    Fi_sri_e_opt = next(x for x in sp.Mul.make_args(Fi_sri_opt)
                       if any(str(y) == sri_e_str for y in x.free_symbols))

    #create instruction set
    sri_instructions = Template(Template("""
<>T = T_arr[j]
<>logPr = log10(${pr_str})
<>X_temp = ${Xeq} {id=X_decl} #this must be a temporary to avoid a race on Fi_temp assignment
<>Fi_temp = ${Fi_sri} {id=Fi_decl, dep=X_decl}
if ${d_str} != 1.0
    Fi_temp = Fi_temp * ${d_eval} {id=Fi_decl1, dep=Fi_decl}
end
if ${e_str} != 0.0
    Fi_temp = Fi_temp * ${e_eval} {id=Fi_decl2, dep=Fi_decl}
end
${Fi_str} = Fi_temp {dep=Fi_decl*}
${X_str} = X_temp
""").safe_substitute(logPr_eval='logPr',
                     pr_str=Pr_str,
                     X_str=X_sri_str,
                     Xeq=X_sri_eq,
                     Fi_sri=Fi_sri_base,
                     d_str=sri_d_str,
                     d_eval=Fi_sri_d_opt,
                     e_str=sri_e_str,
                     e_eval=Fi_sri_e_opt,
                     Fi_str=Fi_str)).safe_substitute(
                            reac_ind=reac_ind)

    return [rateconst_info('fall_sri',
                     instructions=sri_instructions,
                     reac_ind=reac_ind,
                     kernel_data=kernel_data,
                     indicies=indicies,
                     maps=maps)]



def get_simple_arrhenius_rates(eqs, loopy_opt, rate_info, test_size=None,
        falloff=False):
    """Generates instructions, kernel arguements, and data for specialized forms
    of simple (non-pressure dependent) rate constants

    Parameters
    ----------
    eqs : dict
        Sympy equations / variables for constant pressure / constant volume systems
    loopy_opt : `loopy_options` object
        A object containing all the loopy options to execute
    rate_info : dict
        The output of :method:`assign_rates` for this mechanism
    test_size : int
        If not none, this kernel is being used for testing.
        Hence we need to size the arrays accordingly
    falloff : bool
        If true, generate rate kernel for the falloff rates, i.e. either
        k0 or kinf depending on whether the reaction is falloff or chemically activated

    Returns
    -------
    rate_list : list of :class:`rateconst_info`
        The generated infos for feeding into the kernel generator

    """

    #find options, sizes, etc.
    if falloff:
        tag = 'fall'
        kf_name = 'kf_fall'
        Nr = rate_info['fall']['num']
    else:
        tag = 'simple'
        kf_name = 'kf'
        Nr = rate_info['Nr']

    #first assign the reac types, parameters
    full = loopy_opt.rate_spec == lp_utils.RateSpecialization.full
    hybrid = loopy_opt.rate_spec == lp_utils.RateSpecialization.hybrid
    fixed = loopy_opt.rate_spec == lp_utils.RateSpecialization.fixed
    separated_kernels = loopy_opt.rate_spec_kernels
    if fixed and separated_kernels:
        separated_kernels = False
        logging.info('Cannot use separated kernels with a fixed RateSpecialization, '
            'disabling...')

    #define loopy arrays
    A_lp = lp.TemporaryVariable('A', shape=lp.auto,
        initializer=rate_info[tag]['A'],
        read_only=True, scope=scopes.GLOBAL)
    b_lp = lp.TemporaryVariable('beta', shape=lp.auto,
        initializer=rate_info[tag]['b'],
        read_only=True, scope=scopes.GLOBAL)
    Ta_lp = lp.TemporaryVariable('Ta', shape=lp.auto,
        initializer=rate_info[tag]['Ta'],
        read_only=True, scope=scopes.GLOBAL)
    T_arr = lp.GlobalArg('T_arr', shape=(test_size,), dtype=np.float64)
    simple_arrhenius_data = [A_lp, b_lp, Ta_lp, T_arr]

    #if we need the rtype array, add it
    if not separated_kernels and not fixed:
        rtype_lp = lp.TemporaryVariable('rtype', shape=lp.auto,
            initializer=rate_info[tag]['type'],
            read_only=True, scope=scopes.PRIVATE)
        simple_arrhenius_data.append(rtype_lp)

    #the reaction index variable
    reac_ind = 'i'

    maps = []
    #figure out if we need to do any mapping of the input variable
    inmap_name = 'in_map'
    if separated_kernels:
        reac_ind = 'i_dummy'
        maps.append(lp_utils.generate_map_instruction(
                                            newname='i',
                                            map_arr=inmap_name,
                                            oldname=reac_ind))
    #get rate equations
    rate_eqn_pre = get_rate_eqn(eqs)

    #put rateconst info args in dict for unpacking convenience
    extra_args = {'kernel_data' : simple_arrhenius_data,
                  'reac_ind' : reac_ind,
                  'maps' : maps}

    default_preinstructs = [__TINV_PREINST_KEY, __TLOG_PREINST_KEY]

    #generic kf assigment str
    kf_assign = Template("${kf_str} = ${rate}")
    expkf_assign = Template("${kf_str} = exp(${rate})")

    #various specializations of the rate form
    specializations = {}
    i_a_only = rateconst_info(name='a_only',
        instructions=kf_assign.safe_substitute(rate='A[i]'),
        **extra_args)
    i_beta_int = rateconst_info(name='beta_int',
        pre_instructions=[__TINV_PREINST_KEY],
        instructions="""
        <> T_val = T_arr[j] {id=a1}
        <> negval = beta[i] < 0
        if negval
            T_val = T_inv {id=a2, dep=a1}
        end
        ${kf_str} = A[i] * T_val {id=a3, dep=a2}
        ${beta_iter}
        """,
        **extra_args)
    i_beta_exp = rateconst_info('beta_exp',
        instructions=expkf_assign.safe_substitute(rate=str(rate_eqn_pre.subs('Ta[i]', 0))),
        pre_instructions=default_preinstructs,
        **extra_args)
    i_ta_exp = rateconst_info('ta_exp',
        instructions=expkf_assign.safe_substitute(rate=str(rate_eqn_pre.subs('beta[i]', 0))),
        pre_instructions=default_preinstructs,
        **extra_args)
    i_full = rateconst_info('full',
        instructions=expkf_assign.safe_substitute(rate=str(rate_eqn_pre)),
        pre_instructions=default_preinstructs,
        **extra_args)

    #set up the simple arrhenius rate specializations
    if fixed:
        specializations[0] = i_full
    else:
        specializations[0] = i_a_only
        specializations[1] = i_beta_int
        if full:
            specializations[2] = i_beta_exp
            specializations[3] = i_ta_exp
            specializations[4] = i_full
        else:
            specializations[2] = i_full

    if not separated_kernels and not fixed:
        #need to enclose each branch in it's own if statement
        if len(specializations) > 1:
            instruction_list = []
            inds = set()
            for i in specializations:
                instruction_list.append('<>test_{0} = rtype[i] == {0} {{id=d{0}}}'.format(i))
                instruction_list.append('if test_{0}'.format(i))
                instruction_list.extend(['\t' + x for x in specializations[i].instructions.split('\n')])
                instruction_list.append('end')
        #and combine them
        specializations = {-1 : rateconst_info('singlekernel',
            instructions='\n'.join(instruction_list),
            pre_instructions=[__TINV_PREINST_KEY, __TLOG_PREINST_KEY],
            **extra_args)}

    spec_copy = specializations.copy()
    #and do some finalizations for the specializations
    for rtype, info in spec_copy.items():
        #first, get indicies
        if rtype < 0:
            #select all for joined kernel
            info.indicies = np.arange(0, rate_info[tag]['type'].size, dtype=np.int32)
        else:
            #otherwise choose just our rtype
            info.indicies = np.where(rate_info[tag]['type'] == rtype)[0].astype(dtype=np.int32)

        if not info.indicies.size:
            #kernel doesn't act on anything, remove it
            del specializations[rtype]
            continue

        #check maxb / iteration
        beta_iter = ''
        if (separated_kernels and (info.name == i_beta_int.name)) or \
            (not separated_kernels and not fixed):
            #find max b exponent
            maxb_test = rate_info[tag]['b'][
                    np.where(rate_info[tag]['type'] == rtype)]
            if maxb_test.size:
                maxb = int(np.max(np.abs(maxb_test)))
                #if we need to iterate
                if maxb > 1:
                    #add an extra iname, and the resulting iteraton loop
                    info.extra_inames.append(('k', '1 <= maxb < {}'.format(maxb)))
                    beta_iter = """
                <> btest = abs(beta[i])
                for k
                    <>inbounds = k < btest
                    if inbounds
                        ${kf_str} = ${kf_str} * T_val {dep=a2}
                    end
                end"""

        #check if we have an input map
        if info.reac_ind != 'i':
            if info.indicies[0] + info.indicies.size - 1 == info.indicies[-1]:
                #it'll end up in offset form, hence we don't need a map
                info.reac_ind = 'i'
                info.maps = []
            else:
                #need to add the input map to kernel data
                inmap_lp = lp.TemporaryVariable(inmap_name,
                    shape=lp.auto,
                    initializer=info.indicies,
                    read_only=True, scope=scopes.PRIVATE)
                info.kernel_data.append(inmap_lp)

        #check if we need an output map
        out_map = {}
        outmap_name = 'out_map'
        alt_inds = None
        if not falloff:
            alt_inds = rate_info[tag]['map'][info.indicies]
        info.indicies = __handle_indicies(info.indicies, info.reac_ind,
                      out_map, info.kernel_data, outmap_name=outmap_name,
                      alternate_indicies=alt_inds)

        #get the proper kf indexing / array
        kf_arr, kf_str, map_result = lp_utils.get_loopy_arg(kf_name,
                        indicies=[info.reac_ind, 'j'],
                        dimensions=[Nr, test_size],
                        map_name=out_map,
                        order=loopy_opt.order)
        info.kernel_data.append(kf_arr)

        #handle map info
        if info.reac_ind in map_result:
            info.maps.append(map_result[info.reac_ind])

        #substitute in whatever beta_iter / kf_str we found
        info.instructions = Template(
                        Template(info.instructions).safe_substitute(
                            beta_iter=beta_iter)
                    ).safe_substitute(kf_str=kf_str)

    return specializations.values()


def make_rateconst_kernel(info, target, test_size):
    """
    Convience method to create kernels for rate constant evaluation

    Parameters
    ----------
    info : :class:`rateconst_info`
        The rate contstant info to generate the kernel from
    target : :class:`loopy.TargetBase`
        The target to generate code for
    test_size : int/str
        The integer (or symbolic) problem size

    Returns
    -------
    knl : :class::class:`loopy.LoopKernel`
        The generated loopy kernel
    """

    #various precomputes
    pre_inst = {__TINV_PREINST_KEY : '<> T_inv = 1 / T_arr[j]',
                __TLOG_PREINST_KEY : '<> logT = log(T_arr[j])',
                __PLOG_PREINST_KEY : '<> logP = log(P_arr[j])'}

    #and the skeleton kernel
    skeleton = """
    for j
        ${pre}
        for ${reac_ind}
            ${main}
        end
    end
    """

    #convert instructions into a list for convienence
    instructions = info.instructions
    if isinstance(instructions, str):
        instructions = textwrap.dedent(info.instructions)
        instructions = [x for x in instructions.split('\n') if x.strip()]

    #load inames
    inames = [info.reac_ind, 'j']

    #add map instructions
    instructions = info.maps + instructions

    #look for extra inames, ranges
    iname_range = []

    assumptions = info.assumptions[:]

    #find the start index for 'i'
    if isinstance(info.indicies, tuple):
        i_start = info.indicies[0]
        i_end = info.indicies[1]
    else:
        i_start = 0
        i_end = info.indicies.size

    #add to ranges
    iname_range.append('{}<={}<{}'.format(i_start, info.reac_ind, i_end))
    iname_range.append('{}<=j<{}'.format(0, test_size))

    if isinstance(test_size, str):
        assumptions.append('{0} > 0'.format(test_size))

    for iname, irange in info.extra_inames:
        inames.append(iname)
        iname_range.append(irange)

    #construct the kernel args
    pre_instructions = [pre_inst[k] if k in pre_inst else k
                            for k in info.pre_instructions]

    def subs_preprocess(key, value):
        #find the instance of ${key} in kernel_str
        whitespace = None
        for i, line in enumerate(skeleton.split('\n')):
            if key in line:
                #get whitespace
                whitespace = re.match(r'\s*', line).group()
                break
        result = [line if i == 0 else whitespace + line for i, line in
                    enumerate(textwrap.dedent(value).splitlines())]
        return Template('\n'.join(result)).safe_substitute(reac_ind=info.reac_ind)


    kernel_str = Template(skeleton).safe_substitute(
        reac_ind=info.reac_ind,
        pre=subs_preprocess('${pre}', '\n'.join(pre_instructions)),
        main=subs_preprocess('${main}', '\n'.join(instructions)))

    iname_arr = []
    #generate iname strings
    for iname, irange in zip(*(inames,iname_range)):
        iname_arr.append(Template(
            '{[${iname}]:${irange}}').safe_substitute(
            iname=iname,
            irange=irange
            ))

    #make the kernel
    knl = lp.make_kernel(iname_arr,
        kernel_str,
        kernel_data=info.kernel_data,
        name='rateconst_' + info.name,
        target=target,
        assumptions=' and '.join(assumptions)
    )
    #fix parameters
    if info.parameters:
        knl = lp.fix_parameters(knl, **info.parameters)
    #prioritize and return
    knl = lp.prioritize_loops(knl, inames)
    return knl

def apply_rateconst_vectorization(loopy_opt, reac_ind, knl):
    """
    Applies wide / deep vectorization to a generic rateconst kernel

    Parameters
    ----------
    loopy_opt : :class:`loopy_options` object
        A object containing all the loopy options to execute
    reac_ind : str
        The reaction index variable
    knl : :class:`loopy.LoopKernel`
        The kernel to transform

    Returns
    -------
    knl : :class:`loopy.LoopKernel`
        The transformed kernel
    """
    #now apply specified optimizations
    if loopy_opt.depth is not None:
        #and assign the l0 axis to 'i'
        knl = lp.split_iname(knl, reac_ind, loopy_opt.depth, inner_tag='l.0')
        #assign g0 to 'j'
        knl = lp.tag_inames(knl, [('j', 'g.0')])
    elif loopy_opt.width is not None:
        #make the kernel a block of specifed width
        knl = lp.split_iname(knl, 'j', loopy_opt.width, inner_tag='l.0')
        #assign g0 to 'i'
        knl = lp.tag_inames(knl, [('j_outer', 'g.0')])

    #now do unr / ilp
    i_tag = reac_ind + '_outer' if loopy_opt.depth is not None else reac_ind
    if loopy_opt.unr is not None:
        knl = lp.split_iname(knl, i_tag, loopy_opt.unr, inner_tag='unr')
    elif loopy_opt.ilp:
        knl = lp.tag_inames(knl, [(i_tag, 'ilp')])

    return knl

def rate_const_kernel_gen(eqs, reacs, specs,
                            loopy_opt, test_size=None):
    """Helper function that generates kernels for
       evaluation of simple arrhenius rate forms

    Parameters
    ----------
    eqs : dict
        Sympy equations / variables for constant pressure / constant volume systems
    reacs : list of `ReacInfo`
        List of reactions in the mechanism.
    specs : list of `SpecInfo`
        List of species in the mechanism
    loopy_opt : `loopy_options` object
        A object containing all the loopy options to execute
    test_size : int
        If not none, this kernel is being used for testing. Hence we need to size the arrays accordingly

    Returns
    -------
    knl_list : list of :class:`loopy.LoopKernel`
        The generated loopy kernel(s) for code generation / testing

    """

    if test_size is None:
        test_size = 'n'

    #determine rate evaluation types, indicies etc.
    rate_info = assign_rates(reacs, specs, loopy_opt.rate_spec)

    kernels = {}

    #get the simple arrhenius rateconst_info's
    kernels['simple'] = get_simple_arrhenius_rates(eqs, loopy_opt,
        rate_info, test_size=test_size)

    #check for plog
    if any(r.plog for r in reacs):
        #generate the plog kernel
        kernels['plog'] = get_plog_arrhenius_rates(eqs, loopy_opt,
            rate_info, test_size=test_size)

    if any(r.cheb for r in reacs):
        kernels['plog'] = get_cheb_arrhenius_rates(eqs, loopy_opt,
            rate_info, test_size=test_size)


    knl_list = {}
    #now create the kernels!

    for eval_type in kernels:
        knl_list[eval_type] = []
        for info in kernels[eval_type]:
            knl_list[eval_type].append((info.reac_ind,
                make_rateconst_kernel(info, target, test_size)))

        #stub for special handling of various evaluation types here

        #apply other optimizations
        for i, (reac_ind, knl) in enumerate(knl_list[eval_type]):
            knl_list[eval_type][i] = apply_rateconst_vectorization(loopy_opt,
                reac_ind, knl)

    return knl_list

def get_rate_eqn(eqs, index='i'):
    """Helper routine that returns the Arrenhius rate constant in exponential
    form.

    Parameters
    ----------
    eqs : dict
        Sympy equations / variables for constant pressure / constant volume systems
    index : str
        The index to generate the equations for, 'i' by default
    Returns
    -------
    rate_eqn_pre : `sympy.Expr`
        The rate constant before taking the exponential (sympy does odd things upon doing so)
        This is used for various simplifications

    """

    conp_eqs = eqs['conp']

    #define some dummy symbols for loopy writing
    E_sym = sp.Symbol('Ta[{ind}]'.format(ind=index))
    A_sym = sp.Symbol('A[{ind}]'.format(ind=index))
    T_sym = sp.Symbol('T')
    b_sym = sp.Symbol('beta[{ind}]'.format(ind=index))
    symlist = {'Ta[i]' : E_sym,
               'A[i]' : A_sym,
               'T' : T_sym,
               'beta[i]' : b_sym}
    Tinv_sym = sp.Symbol('T_inv')
    logA_sym = sp.Symbol('A[{ind}]'.format(ind=index))
    logT_sym = sp.Symbol('logT')

    #the rate constant is indep. of conp/conv, so just use conp for simplicity
    kf_eqs = [x for x in conp_eqs if str(x) == '{k_f}[i]']

    #do some surgery on the equations
    kf_eqs = {key: (x, conp_eqs[x][key]) for x in kf_eqs for key in conp_eqs[x]}

    #first load the arrenhius rate equation
    rate_eqn = next(kf_eqs[x] for x in kf_eqs if reaction_type.elementary in x)[1]
    rate_eqn = sp_utils.sanitize(rate_eqn,
                        symlist=symlist,
                        subs={sp.Symbol('{E_{a}}[i]') / (sp.Symbol('R_u') * T_sym) :
                            E_sym * Tinv_sym})

    #finally, alter to exponential form:
    rate_eqn_pre = sp.log(A_sym) + sp.log(T_sym) * b_sym - E_sym * Tinv_sym
    rate_eqn_pre = rate_eqn_pre.subs([(sp.log(A_sym), logA_sym),
                                      (sp.log(T_sym), logT_sym)])

    return rate_eqn_pre


def write_rate_constants(path, specs, reacs, eqs, opts, auto_diff=False):
    """Write subroutine(s) to evaluate reaction rates constants.

    Parameters
    ----------
    path : str
        Path to build directory for file.
    specs : list of `SpecInfo`
        List of species in the mechanism.
    reacs : list of `ReacInfo`
        List of reactions in the mechanism.
    eqs : dict
        Sympy equations / variables for constant pressure / constant volume systems
    opts : `loopy_options` object
        A object containing all the loopy options to execute
    auto_diff : bool
        If ``True``, generate files for Adept autodifferention library.
    Returns
    -------
    None

    """

    file_prefix = ''
    double_type = 'double'
    pres_ref = ''
    if auto_diff:
        file_prefix = 'ad_'
        double_type = 'adouble'
        pres_ref = '&'

    target = utils.get_target(opts.lang)

    #generate the rate kernels
    knl_list = rate_const_simple_kernel_gen(eqs, reacs, opts)



def polyfit_kernel_gen(varname, nicename, eqs, specs,
                            loopy_opt, test_size=None):
    """Helper function that generates kernels for
       evaluation of various thermodynamic species properties

    Parameters
    ----------
    varname : str
        The variable to generate the kernel for
    nicename : str
        The variable name to use in generated code
    eqs : dict of `sympy.Symbol`
        Dictionary defining conditional equations for the variables (keys)
    specs : list of `SpecInfo`
        List of species in the mechanism.
    lang : {'c', 'cuda', 'opencl'}
        Programming language.
    loopy_opt : `loopy_options` object
        A object containing all the loopy options to execute
    test_size : int
        If not None, this kernel is being used for testing.

    Returns
    -------
    knl : :class:`loopy.LoopKernel`
        The generated loopy kernel for code generation / testing

    """

    if test_size is None:
        test_size = 'n'

    if loopy_opt.width is not None and loopy_opt.depth is not None:
        raise Exception('Cannot specify both SIMD/SIMT width and depth')

    var = next(v for v in eqs.keys() if str(v) == varname)
    eq = eqs[var]
    poly_dim = specs[0].hi.shape[0]
    Ns = len(specs)

    #pick out a values and T_mid
    a_lo = np.zeros((Ns, poly_dim), dtype=np.float64)
    a_hi = np.zeros((Ns, poly_dim), dtype=np.float64)
    T_mid = np.zeros((Ns,), dtype=np.float64)
    for ind, spec in enumerate(specs):
        a_lo[ind, :] = spec.lo[:]
        a_hi[ind, :] = spec.hi[:]
        T_mid[ind] = spec.Trange[1]

    #get correctly ordered arrays / strings
    indicies = ['k', '${param_val}']
    a_lo_lp = lp.TemporaryVariable('a_lo', shape=a_lo.shape, initializer=a_lo,
        scope=scopes.GLOBAL, read_only=True)
    a_hi_lp = lp.TemporaryVariable('a_hi', shape=a_hi.shape, initializer=a_hi,
        scope=scopes.GLOBAL, read_only=True)
    a_lo_str = Template('a_lo[' + ','.join(indicies) + ']')
    a_hi_str = Template('a_hi[' + ','.join(indicies) + ']')
    T_mid_lp = lp.TemporaryVariable('T_mid', shape=T_mid.shape, initializer=T_mid, read_only=True,
                                scope=scopes.GLOBAL)

    k = sp.Idx('k')
    lo_eq_str = str(eq.subs([(sp.IndexedBase('a')[k, i],
        a_lo_str.safe_substitute(param_val=i)) for i in range(poly_dim)]))
    hi_eq_str = str(eq.subs([(sp.IndexedBase('a')[k, i],
        a_hi_str.safe_substitute(param_val=i)) for i in range(poly_dim)]))

    target = lp_utils.get_target(loopy_opt.lang)

    #create the input arrays arrays
    T_lp = lp.GlobalArg('T_arr', shape=(test_size,), dtype=np.float64)
    out_lp, out_str, _ = lp_utils.get_loopy_arg(nicename,
                    indicies=['k', 'i'],
                    dimensions=(Ns, test_size),
                    order=loopy_opt.order)

    knl_data = [a_lo_lp, a_hi_lp, T_mid_lp, T_lp, out_lp]

    if test_size == 'n':
        knl_data = [lp.ValueArg('n', dtype=np.int32)] + knl_data

    #first we generate the kernel
    knl = lp.make_kernel('{{[k, i]: 0<=k<{} and 0<=i<{}}}'.format(Ns,
        test_size),
        Template("""
        for i
            <> T = T_arr[i]
            for k
                if T < T_mid[k]
                    ${out_str} = ${lo_eq}
                else
                    ${out_str} = ${hi_eq}
                end
            end
        end
        """).safe_substitute(out_str=out_str, lo_eq=lo_eq_str, hi_eq=hi_eq_str),
        target=target,
        kernel_data=knl_data,
        name='eval_{}'.format(nicename)
        )

    knl = lp.prioritize_loops(knl, ['i', 'k'])


    if loopy_opt.depth is not None:
        #and assign the l0 axis to 'k'
        knl = lp.split_iname(knl, 'k', loopy_opt.depth, inner_tag='l.0')
        #assign g0 to 'i'
        knl = lp.tag_inames(knl, [('i', 'g.0')])
    elif loopy_opt.width is not None:
        #make the kernel a block of specifed width
        knl = lp.split_iname(knl, 'i', loopy_opt.width, inner_tag='l.0')
        #assign g0 to 'i'
        knl = lp.tag_inames(knl, [('i_outer', 'g.0')])

    #specify R
    knl = lp.fix_parameters(knl, R_u=chem.RU)

    #now do unr / ilp
    k_tag = 'k_outer' if loopy_opt.depth is not None else 'k'
    if loopy_opt.unr is not None:
        knl = lp.split_iname(knl, k_tag, loopy_opt.unr, inner_tag='unr')
    elif loopy_opt.ilp:
        knl = lp.tag_inames(knl, [(k_tag, 'ilp')])

    return knl


def write_chem_utils(path, specs, eqs, opts, auto_diff=False):
    """Write subroutine to evaluate species thermodynamic properties.

    Notes
    -----
    Thermodynamic properties include:  enthalpy, energy, specific heat
    (constant pressure and volume).

    Parameters
    ----------
    path : str
        Path to build directory for file.
    specs : list of `SpecInfo`
        List of species in the mechanism.
    eqs : dict
        Sympy equations / variables for constant pressure / constant volume systems
    opts : `loopy_options` object
        A object containing all the loopy options to execute
    auto_diff : bool
        If ``True``, generate files for Adept autodifferention library.
    Returns
    -------
    None

    """

    file_prefix = ''
    if auto_diff:
        file_prefix = 'ad_'

    target = lp_utils.get_target(opts.lang)

    #generate the kernels
    conp_eqs = eqs['conp']
    conv_eqs = eqs['conv']


    namelist = ['cp', 'cv', 'h', 'u']
    kernels = []
    headers = []
    code = []
    for varname, nicename in [('{C_p}[k]', 'cp'),
        ('H[k]', 'h'), ('{C_v}[k]', 'cv'),
        ('U[k]', 'u'), ('B[k]', 'b')]:
        eq = conp_eqs if nicename in ['h', 'cp'] else conv_eqs
        kernels.append(polyfit_kernel_gen(varname, nicename,
            eq, specs, opts))

    #get headers
    for i in range(len(namelist)):
        headers.append(lp_utils.get_header(kernels[i]) + utils.line_end[opts.lang])

    #and code
    for i in range(len(namelist)):
        code.append(lp_utils.get_code(kernels[i]))

    #need to filter out double definitions of constants in code
    preamble = []
    inkernel = False
    for line in code[0].split('\n'):
        if not re.search(r'eval_\w+', line):
            preamble.append(line)
        else:
            break
    code = [line for line in '\n'.join(code).split('\n') if line not in preamble]


    #now write
    with filew.get_header_file(os.path.join(path, file_prefix + 'chem_utils'
                             + utils.header_ext[opts.lang]), opts.lang) as file:

        if auto_diff:
            file.add_headers('adept.h')
            file.add_lines('using adept::adouble;\n')
            lines = [x.replace('double', 'adouble') for x in lines]

        lines = '\n'.join(headers).split('\n')
        file.add_lines(lines)


    with filew.get_file(os.path.join(path, file_prefix + 'chem_utils'
                             + utils.file_ext[opts.lang]), opts.lang) as file:
        file.add_lines(preamble + code)


def write_derivs(path, lang, specs, reacs, specs_nonzero, auto_diff=False):
    """Writes derivative function file and header.

    Parameters
    ----------
    path : str
        Path to build directory for file.
    lang : {'c', 'cuda', 'fortran', 'matlab'}
        Programming language.
    specs : list of `SpecInfo`
        List of species in the mechanism.
    reacs : list of `ReacInfo`
        List of reactions in the mechanism.
    specs_nonzero : list of bool
        List of `bool` indicating species with zero net production
    auto_diff : bool, optional
        If ``True``, generate files for Adept autodifferention library.

    Returns
    -------
    None

    """


    file_prefix = ''
    double_type = 'double'
    pres_ref = ''
    if auto_diff:
        file_prefix = 'ad_'
        double_type = 'adouble'
        pres_ref = '&'

    pre = ''
    if lang == 'cuda': pre = '__device__ '

    # first write header file
    file = open(os.path.join(path, file_prefix + 'dydt' +
                            utils.header_ext[lang]), 'w')
    file.write('#ifndef DYDT_HEAD\n'
               '#define DYDT_HEAD\n'
               '\n'
               '#include "header{}"\n'.format(utils.header_ext[lang]) +
               '\n'
               )
    if auto_diff:
        file.write('#include "adept.h"\n'
                   'using adept::adouble;\n')
    file.write('{0}void dydt (const double, const {1}{2}, '
               'const {1} * {3}, {1} * {3}'.format(pre, double_type, pres_ref,
                                utils.restrict[lang]) +
               ('' if lang == 'c' else
                    ', const mechanism_memory * {}'.format(utils.restrict[lang])) +
               ');\n'
               '\n'
               '#endif\n'
               )
    file.close()

    filename = file_prefix + 'dydt' + utils.file_ext[lang]
    file = open(os.path.join(path, filename), 'w')

    file.write('#include "header{}"\n'.format(utils.header_ext[lang]))

    file.write('#include "{0}chem_utils{1}"\n'
               '#include "{0}rates{1}"\n'.format(file_prefix,
                                            utils.header_ext[lang]))
    if lang == 'cuda':
        file.write('#include "gpu_memory.cuh"\n'
                   )
    file.write('\n')
    if auto_diff:
        file.write('#include "adept.h"\n'
                   'using adept::adouble;\n')

    ##################################################################
    # constant pressure
    ##################################################################
    file.write('#if defined(CONP)\n\n')

    line = (pre + 'void dydt (const double t, const {0}{1} pres, '
                  'const {0} * {2} y, {0} * {2} dy{3}) {{\n\n'.format(
                  double_type, pres_ref, utils.restrict[lang],
                  ', const mechanism_memory * {} d_mem'.format(utils.restrict[lang])
                  if lang == 'cuda' else '')
            )
    file.write(line)

    # calculation of species molar concentrations
    file.write('  // species molar concentrations\n')
    file.write(('  {0} conc[{1}]'.format(double_type, len(specs)) if lang != 'cuda'
               else '  double * {} conc = d_mem->conc'.format(utils.restrict[lang]))
               + utils.line_end[lang]
               )

    file.write('  {0} y_N;\n'.format(double_type))
    file.write('  {0} mw_avg;\n'.format(double_type))
    file.write('  {0} rho;\n'.format(double_type))

    # Simply call subroutine
    file.write('  eval_conc (' + utils.get_array(lang, 'y', 0) +
               ', pres, &' + (utils.get_array(lang, 'y', 1) if lang != 'cuda'
                                else 'y[GRID_DIM]') + ', '
               '&y_N, &mw_avg, &rho, conc);\n\n'
               )

    # evaluate reaction rates
    rev_reacs = [i for i, rxn in enumerate(reacs) if rxn.rev]
    if lang == 'cuda':
        file.write('  double * {} fwd_rates = d_mem->fwd_rates'.format(utils.restrict[lang])
                   + utils.line_end[lang])
    else:
        file.write('  // local arrays holding reaction rates\n'
                   '  {} fwd_rates[{}];\n'.format(double_type, len(reacs))
                   )
    if rev_reacs and lang == 'cuda':
        file.write('  double * {} rev_rates = d_mem->rev_rates'.format(utils.restrict[lang])
                   + utils.line_end[lang])
    elif rev_reacs:
        file.write('  {} rev_rates[{}];\n'.format(double_type, len(rev_reacs)))
    else:
        file.write('  {}* rev_rates = 0;\n'.format(double_type))
    cheb = False
    if any(rxn.cheb for rxn in reacs) and lang == 'cuda':
        cheb = True
        file.write('  double * {} dot_prod = d_mem->dot_prod'.format(utils.restrict[lang])
                   + utils.line_end[lang])
    file.write('  eval_rxn_rates (' + utils.get_array(lang, 'y', 0) +
               ', pres, conc, fwd_rates, rev_rates{});\n\n'.format(', dot_prod'
                    if cheb else '')
               )

    # reaction pressure dependence
    num_dep_reacs = sum([rxn.thd_body or rxn.pdep for rxn in reacs])
    if num_dep_reacs > 0:
        file.write('  // get pressure modifications to reaction rates\n')
        if lang == 'cuda':
            file.write('  double * {} pres_mod = d_mem->pres_mod'.format(utils.restrict[lang]) +
                   utils.line_end[lang])
        else:
            file.write('  {} pres_mod[{}];\n'.format(double_type, num_dep_reacs))
        file.write('  get_rxn_pres_mod (' + utils.get_array(lang, 'y', 0) +
                   ', pres, conc, pres_mod);\n'
                   )
    else:
        file.write('  {}* pres_mod = 0;\n'.format(double_type))
    file.write('\n')

    if lang == 'cuda':
        file.write('  double * {} spec_rates = d_mem->spec_rates'.format(utils.restrict[lang]) +
                   utils.line_end[lang])

    # species rate of change of molar concentration
    file.write('  // evaluate species molar net production rates\n')
    if lang != 'cuda':
        file.write('  {} dy_N;\n'.format(double_type))
    file.write('  eval_spec_rates (fwd_rates, rev_rates, pres_mod, ')
    if lang == 'c':
        file.write('&' + utils.get_array(lang, 'dy', 1) + ', &dy_N)')
    elif lang == 'cuda':
        file.write('spec_rates, &' + utils.get_array(lang, 'spec_rates', len(specs) - 1) +
                    ')')
    file.write(utils.line_end[lang])

    # evaluate specific heat
    file.write('  // local array holding constant pressure specific heat\n')
    file.write(('  {} cp[{}]'.format(double_type, len(specs)) if lang != 'cuda'
               else '  double * {} cp = d_mem->cp'.format(utils.restrict[lang]))
               + utils.line_end[lang])
    file.write('  eval_cp (' + utils.get_array(lang, 'y', 0) + ', cp)'
               + utils.line_end[lang] + '\n')

    file.write('  // constant pressure mass-average specific heat\n')
    line = '  {} cp_avg = '.format(double_type)
    isfirst = True
    for isp, sp in enumerate(specs[:-1]):
        if len(line) > 70:
            line += '\n'
            file.write(line)
            line = '             '

        if not isfirst: line += ' + '

        line += '(' + utils.get_array(lang, 'cp', isp) + \
                ' * ' + utils.get_array(lang, 'y', isp + 1) + ')'

        isfirst = False

    if not isfirst: line += ' + '
    line += '(' + utils.get_array(lang, 'cp', len(specs) - 1) + ' * y_N)'
    file.write(line + utils.line_end[lang] + '\n')

    file.write('  // local array for species enthalpies\n' +
              ('  {} h[{}]'.format(double_type, len(specs)) if lang != 'cuda'
               else '  double * {} h = d_mem->h'.format(utils.restrict[lang]))
               + utils.line_end[lang])
    file.write('  eval_h(' + utils.get_array(lang, 'y', 0) + ', h);\n')

    # energy equation
    file.write('  // rate of change of temperature\n')
    line = ('  ' + utils.get_array(lang, 'dy', 0) +
            ' = (-1.0 / (rho * cp_avg)) * ('
            )
    isfirst = True
    for isp, sp in enumerate(specs):
        if not specs_nonzero[isp]:
            continue
        if len(line) > 70:
            line += '\n'
            file.write(line)
            line = '       '

        if not isfirst: line += ' + '

        if lang == 'c':
            arr = utils.get_array(lang, 'dy', isp + 1) if isp < len(specs) - 1 else 'dy_N'
        elif lang == 'cuda':
            arr = utils.get_array(lang, 'spec_rates', isp)
        line += ('(' + arr + ' * ' +
                 utils.get_array(lang, 'h', isp) + ' * {:.16e})'.format(sp.mw)
                 )

        isfirst = False
    line += ')' + utils.line_end[lang] + '\n'
    file.write(line)


    line = ''
    # rate of change of species mass fractions
    file.write('  // calculate rate of change of species mass fractions\n')
    for isp, sp in enumerate(specs[:-1]):
        if lang == 'c':
            file.write('  ' + utils.get_array(lang, 'dy', isp + 1) +
                   ' *= ({:.16e} / rho);\n'.format(sp.mw)
                   )
        elif lang == 'cuda':
            file.write('  ' + utils.get_array(lang, 'dy', isp + 1) +
                       ' = ' + utils.get_array(lang, 'spec_rates', isp) +
                       ' * ({:.16e} / rho);\n'.format(sp.mw)
                       )
    file.write('\n')

    file.write('} // end dydt\n\n')

    ##################################################################
    # constant volume
    ##################################################################
    file.write('#elif defined(CONV)\n\n')

    file.write(pre + 'void dydt (const double t, const {0} rho, '
                     'const {0} * {1} y, {0} * {1} dy{2}) {{\n\n'.format(double_type,
                     utils.restrict[lang],
                     '' if lang != 'cuda' else
                     ', mechanism_memory * {} d_mem'.format(utils.restrict[lang]))
               )

    # calculation of species molar concentrations
    file.write('  // species molar concentrations\n')
    file.write(('  {0} conc[{1}]'.format(double_type, len(specs)) if lang != 'cuda'
               else '  double * {} conc = d_mem->conc'.format(utils.restrict[lang]))
               + utils.line_end[lang]
               )

    file.write('  {} y_N;\n'.format(double_type))
    file.write('  {} mw_avg;\n'.format(double_type))
    file.write('  {} pres;\n'.format(double_type))

    # Simply call subroutine
    file.write('  eval_conc_rho (' + utils.get_array(lang, 'y', 0) +
               'rho, &' + (utils.get_array(lang, 'y', 1) if lang != 'cuda'
                                else 'y[GRID_DIM]') + ', ' +
               '&y_N, &mw_avg, &pres, conc);\n\n'
               )

    # evaluate reaction rates
    rev_reacs = [i for i, rxn in enumerate(reacs) if rxn.rev]
    if lang == 'cuda':
        file.write('  double * {} fwd_rates = d_mem->fwd_rates'.format(utils.restrict[lang])
                   + utils.line_end[lang])
    else:
        file.write('  // local arrays holding reaction rates\n'
                   '  {} fwd_rates[{}];\n'.format(double_type, len(reacs))
                   )
    if rev_reacs and lang == 'cuda':
        file.write('  double * {} rev_rates = d_mem->rev_rates'.format(utils.restrict[lang])
                   + utils.line_end[lang])
    elif rev_reacs:
        file.write('  {} rev_rates[{}];\n'.format(double_type, len(rev_reacs)))
    else:
        file.write('  {}* rev_rates = 0;\n'.format(double_type))
    file.write('  eval_rxn_rates (' + utils.get_array(lang, 'y', 0) + ', '
               'pres, conc, fwd_rates, rev_rates);\n\n'
               )

    # reaction pressure dependence
    num_dep_reacs = sum([rxn.thd_body or rxn.pdep for rxn in reacs])
    if num_dep_reacs > 0:
        file.write('  // get pressure modifications to reaction rates\n')
        if lang == 'cuda':
            file.write('  double * {} pres_mod = d_mem->pres_mod'.format(utils.restrict[lang]) +
                   utils.line_end[lang])
        else:
            file.write('  {} pres_mod[{}];\n'.format(double_type, num_dep_reacs))
        file.write('  get_rxn_pres_mod (' + utils.get_array(lang, 'y', 0) +
                   ', pres, conc, pres_mod);\n'
                   )
    else:
        file.write('  {}* pres_mod = 0;\n'.format(double_type))
    file.write('\n')

    # species rate of change of molar concentration
    file.write('  // evaluate species molar net production rates\n'
               '  {} dy_N;'.format(double_type) +
               '  eval_spec_rates (fwd_rates, rev_rates, pres_mod, ')
    file.write('&' + utils.get_array(lang, 'dy', 1) if lang != 'cuda'
           else '&dy[GRID_DIM]')
    file.write(', &dy_N)' + utils.line_end[lang] + '\n')

    # evaluate specific heat
    file.write(('  {} cv[{}]'.format(double_type, len(specs)) if lang != 'cuda'
               else '  double * {} cv = d_mem->cp'.format(utils.restrict[lang]))
               + utils.line_end[lang])
    file.write('  eval_cv(' + utils.get_array(lang, 'y', 0) + ', cv);\n\n')

    file.write('  // constant volume mass-average specific heat\n')
    line = '  {} cv_avg = '.format(double_type)
    isfirst = True
    for idx, sp in enumerate(specs[:-1]):
        if len(line) > 70:
            line += '\n'
            file.write(line)
            line = '             '
        line += ' + ' if not isfirst else ''
        line += ('(' + utils.get_array(lang, 'cv', idx) + ' * ' +
                 utils.get_array(lang, 'y', idx + 1) + ')'
                 )

        isfirst = False
    line += '(' + utils.get_array(lang, 'cv', len(specs) - 1) + ' * y_N)'
    file.write(line + utils.line_end[lang] + '\n')

    # evaluate internal energy
    file.write('  // local array for species internal energies\n' +
              ('  {} u[{}]'.format(double_type, len(specs)) if lang != 'cuda'
               else '  double * {} u = d_mem->h'.format(utils.restrict[lang]))
               + utils.line_end[lang])
    file.write('  eval_u (' + utils.get_array(lang, 'y', 0) + ', u);\n\n')

    # energy equation
    file.write('  // rate of change of temperature\n')
    line = ('  ' + utils.get_array(lang, 'dy', 0) +
            ' = (-1.0 / (rho * cv_avg)) * ('
            )
    isfirst = True
    for isp, sp in enumerate(specs):
        if not specs_nonzero[isp]:
            continue
        if len(line) > 70:
            line += '\n'
            file.write(line)
            line = '       '

        if not isfirst: line += ' + '

        if lang == 'c':
            arr = utils.get_array(lang, 'dy', isp + 1) if isp < len(specs) - 1 else 'dy_N'
        elif lang == 'cuda':
            arr = utils.get_array(lang, 'spec_rates', isp)
        line += ('(' + arr + ' * ' +
                 utils.get_array(lang, 'u', isp) + ' * {:.16e})'.format(sp.mw)
                 )

        isfirst = False
    line += ')' + utils.line_end[lang] + '\n'
    file.write(line)


    # rate of change of species mass fractions
    file.write('  // calculate rate of change of species mass fractions\n')
    for isp, sp in enumerate(specs[:-1]):
        if lang == 'c':
            file.write('  ' + utils.get_array(lang, 'dy', isp + 1) +
                   ' *= ({:.16e} / rho);\n'.format(sp.mw)
                   )
        elif lang == 'cuda':
            file.write('  ' + utils.get_array(lang, 'dy', isp + 1) +
                       ' = ' + utils.get_array(lang, 'spec_rates', isp) +
                       ' * ({:.16e} / rho);\n'.format(sp.mw)
                       )

    file.write('\n')

    file.write('} // end dydt\n\n')

    file.write('#endif\n')

    file.close()
    return


def write_mass_mole(path, lang, specs):
    """Write files for mass/molar concentration and density conversion utility.

    Parameters
    ----------
    path : str
        Path to build directory for file.
    lang : {'c', 'cuda', 'fortran', 'matlab'}
        Programming language.
    specs : list of `SpecInfo`
        List of species in mechanism.

    Returns
    -------
    None

    """

    # Create header file
    if lang in ['c', 'cuda']:
        arr_lang = 'c'
        file = open(os.path.join(path, 'mass_mole{}'.format(
            utils.header_ext[lang])), 'w')

        file.write(
            '#ifndef MASS_MOLE_HEAD\n'
            '#define MASS_MOLE_HEAD\n'
            '\n'
            '#include "header{0}"\n'
            '\n'
            'void mole2mass (const double*, double*);\n'
            'void mass2mole (const double*, double*);\n'
            'double getDensity (const double, const double, const double*);\n'
            '\n'
            '#endif\n'.format(utils.header_ext[lang])
            )
        file.close()

    # Open file; both C and CUDA programs use C file (only used on host)
    filename = 'mass_mole' + utils.file_ext[lang]
    file = open(os.path.join(path, filename), 'w')

    if lang in ['c', 'cuda']:
        file.write('#include "mass_mole{}"\n\n'.format(
            utils.header_ext[lang]))

    ###################################################
    # Documentation and function/subroutine initializaton for mole2mass
    if lang in ['c', 'cuda']:
        file.write('/** Function converting species mole fractions to '
                   'mass fractions.\n'
                   ' *\n'
                   ' * \param[in]  X  array of species mole fractions\n'
                   ' * \param[out] Y  array of species mass fractions\n'
                   ' */\n'
                   'void mole2mass (const double * X, double * Y) {\n'
                   '\n'
                   )
    elif lang == 'fortran':
        file.write(
        '!-----------------------------------------------------------------\n'
        '!> Subroutine converting species mole fractions to mass fractions.\n'
        '!! @param[in]  X  array of species mole fractions\n'
        '!! @param[out] Y  array of species mass fractions\n'
        '!-----------------------------------------------------------------\n'
        'subroutine mole2mass (X, Y)\n'
        '  implicit none\n'
        '  double, dimension(:), intent(in) :: X\n'
        '  double, dimension(:), intent(out) :: X\n'
        '  double :: mw_avg\n'
        '\n'
        )

    file.write('  // mole fraction of final species\n')
    file.write(utils.line_start + 'double X_N' + utils.line_end[lang])
    line = '  X_N = 1.0 - ('
    isfirst = True
    for isp in range(len(specs) - 1):
        if len(line) > 70:
            line += '\n'
            file.write(line)
            line = '               '

        if not isfirst: line += ' + '

        line += utils.get_array(arr_lang, 'X', isp)

        isfirst = False
    line += ')'
    file.write(line + utils.line_end[lang])

    # calculate molecular weight
    if lang in ['c', 'cuda']:
        file.write('  // average molecular weight\n'
                   '  double mw_avg = 0.0;\n'
                   )
        for isp in range(len(specs) - 1):
            sp = specs[isp]
            file.write('  mw_avg += ' + utils.get_array(arr_lang, 'X', isp) +
                       ' * {:.16e};\n'.format(sp.mw)
                       )
        file.write(utils.line_start + 'mw_avg += X_N * ' +
                       '{:.16e};\n'.format(specs[-1].mw)
                       )
    elif lang == 'fortran':
        file.write('  ! average molecular weight\n'
                   '  mw_avg = 0.0\n'
                   )
        for isp, sp in enumerate(specs):
            file.write('  mw_avg = mw_avg + '
                       'X({}) * '.format(isp + 1) +
                       '{:.16e}\n'.format(sp.mw)
                       )
    file.write('\n')

    # calculate mass fractions
    if lang in ['c', 'cuda']:
        file.write('  // calculate mass fractions\n')
        for isp in range(len(specs) - 1):
            sp = specs[isp]
            file.write('  ' + utils.get_array(arr_lang, 'Y', isp) +
                        ' = ' +
                        utils.get_array(arr_lang, 'X', isp) +
                       ' * {:.16e} / mw_avg;\n'.format(sp.mw)
                       )
        file.write('\n'
                   '} // end mole2mass\n'
                   '\n'
                   )
    elif lang == 'fortran':
        file.write('  ! calculate mass fractions\n')
        for isp, sp in enumerate(specs):
            file.write('  Y({0}) = X({0}) * '.format(isp + 1) +
                       '{:.16e} / mw_avg\n'.format(sp.mw)
                       )
        file.write('\n'
                   'end subroutine mole2mass\n'
                   '\n'
                   )

    ################################
    # Documentation and function/subroutine initialization for mass2mole

    if lang in ['c', 'cuda']:
        file.write('/** Function converting species mass fractions to mole '
                   'fractions.\n'
                   ' *\n'
                   ' * \param[in]  Y  array of species mass fractions\n'
                   ' * \param[out] X  array of species mole fractions\n'
                   ' */\n'
                   'void mass2mole (const double * Y, double * X) {\n'
                   '\n'
                   )
    elif lang == 'fortran':
        file.write('!-------------------------------------------------------'
                   '----------\n'
                   '!> Subroutine converting species mass fractions to mole '
                   'fractions.\n'
                   '!! @param[in]  Y  array of species mass fractions\n'
                   '!! @param[out] X  array of species mole fractions\n'
                   '!-------------------------------------------------------'
                   '----------\n'
                   'subroutine mass2mole (Y, X)\n'
                   '  implicit none\n'
                   '  double, dimension(:), intent(in) :: Y\n'
                   '  double, dimension(:), intent(out) :: X\n'
                   '  double :: mw_avg\n'
                   '\n'
                   )

    # calculate Y_N
    file.write('  // mass fraction of final species\n')
    file.write(utils.line_start + 'double Y_N' + utils.line_end[lang])
    line = '  Y_N = 1.0 - ('
    isfirst = True
    for isp in range(len(specs) - 1):
        if len(line) > 70:
            line += '\n'
            file.write(line)
            line = '               '

        if not isfirst: line += ' + '

        line += utils.get_array(arr_lang, 'Y', isp)

        isfirst = False
    line += ')'
    file.write(line + utils.line_end[lang])

    # calculate average molecular weight
    if lang in ['c', 'cuda']:
        file.write('  // average molecular weight\n')
        file.write('  double mw_avg = 0.0;\n')
        for isp in range(len(specs) - 1):
            file.write('  mw_avg += ' + utils.get_array(arr_lang, 'Y', isp) +
                       ' / {:.16e};\n'.format(specs[isp].mw)
                       )
        file.write('  mw_avg += Y_N / ' +
                       '{:.16e};\n'.format(specs[-1].mw)
                       )
        file.write('  mw_avg = 1.0 / mw_avg;\n')
    elif lang == 'fortran':
        file.write('  ! average molecular weight\n')
        file.write('  mw_avg = 0.0\n')
        for isp, sp in enumerate(specs):
            file.write('  mw_avg = mw_avg + '
                       'Y({}) / '.format(isp + 1) +
                       '{:.16e}\n'.format(sp.mw)
                       )
    file.write('\n')

    # calculate mole fractions
    if lang in ['c', 'cuda']:
        file.write('  // calculate mole fractions\n')
        for isp in range(len(specs) - 1):
            file.write('  ' + utils.get_array(arr_lang, 'X', isp)
                      + ' = ' +
                      utils.get_array(arr_lang, 'Y', isp) +
                       ' * mw_avg / {:.16e};\n'.format(specs[isp].mw)
                       )
        file.write('\n'
                   '} // end mass2mole\n'
                   '\n'
                   )
    elif lang == 'fortran':
        file.write('  ! calculate mass fractions\n')
        for isp, sp in enumerate(specs):
            file.write('  X({0}) = Y({0}) * '.format(isp + 1) +
                       'mw_avg / {:.16e}\n'.format(sp.mw)
                       )
        file.write('\n'
                   'end subroutine mass2mole\n'
                   '\n'
                   )

    ###############################
    # Documentation and subroutine/function initialization for getDensity

    if lang in ['c', 'cuda']:
        file.write('/** Function calculating density from mole fractions.\n'
                   ' *\n'
                   ' * \param[in]  temp  temperature\n'
                   ' * \param[in]  pres  pressure\n'
                   ' * \param[in]  X     array of species mole fractions\n'
                   r' * \return     rho  mixture mass density' + '\n'
                   ' */\n'
                   'double getDensity (const double temp, const double '
                   'pres, '
                   'const double * X) {\n'
                   '\n'
                   )
    elif lang == 'fortran':
        file.write('!-------------------------------------------------------'
                   '----------\n'
                   '!> Function calculating density from mole fractions.\n'
                   '!! @param[in]  temp  temperature\n'
                   '!! @param[in]  pres  pressure\n'
                   '!! @param[in]  X     array of species mole fractions\n'
                   '!! @return     rho   mixture mass density' + '\n'
                   '!-------------------------------------------------------'
                   '----------\n'
                   'function mass2mole (temp, pres, X) result(rho)\n'
                   '  implicit none\n'
                   '  double, intent(in) :: temp, pres\n'
                   '  double, dimension(:), intent(in) :: X\n'
                   '  double :: mw_avg, rho\n'
                   '\n'
                   )

    file.write('  // mole fraction of final species\n')
    file.write(utils.line_start + 'double X_N' + utils.line_end[lang])
    line = '  X_N = 1.0 - ('
    isfirst = True
    for isp in range(len(specs) - 1):
        if len(line) > 70:
            line += '\n'
            file.write(line)
            line = '               '

        if not isfirst: line += ' + '

        line += utils.get_array(arr_lang, 'X', isp)

        isfirst = False
    line += ')'
    file.write(line + utils.line_end[lang])

    # get molecular weight
    if lang in ['c', 'cuda']:
        file.write('  // average molecular weight\n'
                   '  double mw_avg = 0.0;\n'
                   )
        for isp in range(len(specs) - 1):
            file.write('  mw_avg += ' + utils.get_array(arr_lang, 'X', isp) +
                       ' * {:.16e};\n'.format(specs[isp].mw)
                       )
        file.write(utils.line_start + 'mw_avg += X_N * ' +
               '{:.16e};\n'.format(specs[-1].mw))
        file.write('\n')
    elif lang == 'fortran':
        file.write('  ! average molecular weight\n'
                   '  mw_avg = 0.0\n'
                   )
        for isp, sp in enumerate(specs):
            file.write('  mw_avg = mw_avg + '
                       'X({}) * '.format(isp + 1) +
                       '{:.16e}\n'.format(sp.mw)
                       )
        file.write('\n')

    # calculate density
    if lang in ['c', 'cuda']:
        file.write('  return pres * mw_avg / ({:.8e} * temp);'.format(chem.RU))
        file.write('\n')
    else:
        line = '  rho = pres * mw_avg / ({:.8e} * temp)'.format(chem.RU)
        line += utils.line_end[lang]
        file.write(line)

    if lang in ['c', 'cuda']:
        file.write('} // end getDensity\n\n')
    elif lang == 'fortran':
        file.write('end function getDensity\n\n')

    file.close()
    return

if __name__ == "__main__":
    from . import create_jacobian
    args = utils.get_parser()
    create_jacobian(lang=args.lang,
                mech_name=args.input,
                therm_name=args.thermo,
                optimize_cache=args.cache_optimizer,
                initial_state=args.initial_conditions,
                num_blocks=args.num_blocks,
                num_threads=args.num_threads,
                no_shared=args.no_shared,
                L1_preferred=args.L1_preferred,
                multi_thread=args.multi_thread,
                force_optimize=args.force_optimize,
                build_path=args.build_path,
                skip_jac=True,
                last_spec=args.last_species,
                auto_diff=args.auto_diff
                )

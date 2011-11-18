# Author: Anand Patil
# Date: 6 Feb 2011
# License: GPL (see LICENSE)
############################

import numpy as np
import pymc as pm
import gc
from map_utils import *
from generic_mbg import *
import generic_mbg
from g6pd import cut_matern, cut_gaussian
import scipy
from scipy.stats import distributions

# The parameterization of the cut between western and eastern hemispheres.
#
# t = np.linspace(0,1,501)
# 
# def latfun(t):
#     if t<.5:
#         return (t*4-1)*np.pi
#     else:
#         return ((1-t)*4-1)*np.pi
#         
# def lonfun(t):
#     if t<.25:
#         return -28*np.pi/180.
#     elif t < .5:
#         return -28*np.pi/180. + (t-.25)*3.5
#     else:
#         return -169*np.pi/180.
#     
# lat = np.array([latfun(tau)*180./np.pi for tau in t])    
# lon = np.array([lonfun(tau)*180./np.pi for tau in t])

constrained = True
threshold_val = 0.0001
max_p_above = 0.0001

def mean_fn(x,m):
    return pm.gp.zero_fn(x)+m
    
def p_fem_def(p,h):
    return p**2 + 2*p*(1-p)*h

def make_model(lon,lat,input_data,covariate_keys,n_male,male_pos,n_fem,fem_pos):
    """
    This function is required by the generic MBG code.
    """
    
    # How many nuggeted field points to handle with each step method
    grainsize = 10

    # Unique data locations
    data_mesh, logp_mesh, fi, ui, ti = uniquify(lon,lat)
    
    a = pm.Exponential('a', .01, value=1)
    b = pm.Exponential('b', .01, value=1)
    
        
    init_OK = False
    while not init_OK:
        try:
            # The partial sill.
            amp = pm.Exponential('amp', .1, value=1.)

            # The range parameters. Units are RADIANS. 
            # 1 radian = the radius of the earth, about 6378.1 km
            scale = pm.Exponential('scale', .1, value=.08)
	    @pm.potential
	    def scale_constraint (scale=scale) :
	    	if scale>.5:
	    		return -np.inf
    		else:
    			return 0

            # This parameter controls the degree of differentiability of the field.
            diff_degree = pm.Uniform('diff_degree', .01, 3, value=0.5, observed=True)

            # The nugget variance.
            V = pm.Exponential('V', .1, value=.1)

            #@pm.potential
            #def V_constraint(V=V):
            #    if V<.1:
            #        return -np.inf
            #    else:
            #        return 0
            
            coef = np.array([-0.2324802, 0.82773152, -0.01368267, 0.00268184])


            def poly(x,coef=coef):
                return np.sum([c_*x**(power) for (power, c_) in enumerate(coef)], axis=0)

            def linkfn(x, j=[0,0], coef=coef):
                return pm.flib.stukel_invlogit(poly(x,coef), *j)

            def inverse_poly(y, coef=coef):
                poly = coef[::-1] + np.array([0,0,0,-y])
                roots = filter(lambda x: not x.imag, np.roots(poly))
                return np.array(roots).real

            def inverse_linkfn(y, j=[0,0], coef=coef, range=range):
                all_sol = inverse_poly(pm.flib.stukel_logit(y, *j), coef)
                # return all_sol[np.argmin(np.abs(all_sol))]
                if len(all_sol)>1:
                    raise RuntimeError
                return all_sol[0]

            j0 = pm.Normal('j0',0,.1,value=0,observed=True)
            # j1 limits mixing.
            j1 = pm.Normal('j1',0,.1,value=0,observed=True)
            j = pm.Lambda('j',lambda j0=j0,j1=j1: [j0,j1])
            
            m = pm.Uninformative('m',value=-25)
            @pm.deterministic(trace=False)
            def M(m=m):
                return pm.gp.Mean(mean_fn, m=m)
                
            if constrained:
                @pm.potential
                def pripred_check(m=m,amp=amp,V=V):
                    p_above = scipy.stats.distributions.norm.cdf(m-pm.logit(threshold_val), 0, np.sqrt(amp**2+V))
                    if p_above <= max_p_above:
                        return 0.
                    else:
                        return -np.inf
        

            # Create the covariance & its evaluation at the data locations.
            facdict = dict([(k,1.e6) for k in covariate_keys])
            facdict['m'] = 0
            @pm.deterministic(trace=False)
            def C(amp=amp, scale=scale, diff_degree=diff_degree, ck=covariate_keys, id=input_data, ui=ui, facdict=facdict):
                """A covariance function created from the current parameter values."""
                eval_fn = CovarianceWithCovariates(cut_matern, id, ck, ui, fac=facdict)
                return pm.gp.FullRankCovariance(eval_fn, amp=amp, scale=scale, diff_degree=diff_degree)

            sp_sub = pm.gp.GPSubmodel('sp_sub', M, C, logp_mesh, tally_f=False)
                
            init_OK = True
        except pm.ZeroProbability:
            init_OK = False
            cls,inst,tb = sys.exc_info()
            print 'Restarting, message %s\n'%inst.message

    # Make f start somewhere a bit sane
    sp_sub.f_eval.value = sp_sub.f_eval.value - np.mean(sp_sub.f_eval.value)

    # Loop over data clusters
    eps_p_f_d = []
    s_d = []
    male_d = []
    het_def_d = []
    fem_d = []

    for i in xrange(len(male_pos)/grainsize+1):
        sl = slice(i*grainsize,(i+1)*grainsize,None)        
        if len(male_pos[sl])>0:
            # Nuggeted field in this cluster
            eps_p_f_d.append(pm.Normal('eps_p_f_%i'%i, sp_sub.f_eval[fi[sl]], 1./V, trace=False))            

            # The allele frequency
            s_d.append(pm.Lambda('s_%i'%i,lambda lt=eps_p_f_d[-1]: invlogit(lt), trace=False))
            
            where_male = np.where(True-np.isnan(n_male[sl]))[0]
            where_fem = np.where(True-np.isnan(n_fem[sl]))[0]
            if len(where_male) > 0:
                male_d.append(pm.Binomial('male_%i'%i, n_male[sl][where_male], s_d[-1][where_male], value=male_pos[sl][where_male], observed=True))
            if len(where_fem) > 0:
                het_def_d.append(pm.Beta('het_def_%i'%i, alpha=a, beta=b, size=len(where_fem), trace=False))
                p = s_d[-1][where_fem]
                p_def = pm.Lambda('p_def', lambda p=p, h=het_def_d[-1]: p_fem_def(p, h), trace=False)
                fem_d.append(pm.Binomial('fem_%i'%i, n_fem[sl][where_fem], p_def, value=fem_pos[sl][where_fem], observed=True))
    
    # The field plus the nugget
    @pm.deterministic
    def eps_p_f(eps_p_fd = eps_p_f_d):
        """Concatenated version of eps_p_f, for postprocessing & Gibbs sampling purposes"""
        return np.hstack(eps_p_fd)

    # The heterozygote deficiency
    @pm.deterministic
    def het_def(het_def_d = het_def_d):
        return np.hstack(het_def_d)
            
    return locals()

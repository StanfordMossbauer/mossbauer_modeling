from scipy.integrate import quad, quad_vec
from scipy.stats import cauchy, binom, poisson
from scipy.optimize import curve_fit, minimize
from scipy.special import jv

import numpy as np
import matplotlib.pyplot as plt
from tqdm import tqdm
import pandas as pd
from os.path import join

from .utils import *
from mossbauer import physics

class MossbauerMaterial:
    """Parent class for a Mossbauer isotope
    
    Mainly because source and absorber might share certain things
    or even be the same object eventually. So trying to be maximally
    general. For now, a child class just needs a function additional_pars()
    that returns a list of required inputs.

    For now this is pretty useless...
    """
    def __init__(self, p):
        # assert that all required pars were supplied
        # then update the dict with them (and no others)
        required_pars = [
            'Eres',
            'linewidth',
        ] + self.additional_pars()
        check_pars(p, self.required_pars())
        # use this instead of self.__dict__.update() to ignore extras
        for par in required_pars:
            setattr(self, par, p[par])
        return

class MossbauerSource(MossbauerMaterial):
    """Mossbauer source class"""
    def additional_pars(self):
        return ['total_activity']

    def spectrum(self, E):
        """Returns photons/sec at the given energy"""
        return self.total_activity * lorentzian_norm(
            E, self.Eres, self.linewidth/2
        )

class MossbauerAbsorber(MossbauerMaterial):
    """Mossbauer absorber class"""
    def additional_pars(self):
        """Require number of mean free paths thick"""
        return  ['thickness_normalized']

    def cross_section(self, E, vel=0.0):
        """Returns resonant absorption cross-section at the given energy"""
        return lorentzian(
            E, 
            self.Eres - vel,
            self.linewidth/2,
        )

class MossbauerMeasurement:
    """Wrapper class for a Mossbauer scan
    
    Owns a MossbauerSource instance and a MossbauerAbsorber instance.
    For now this houses everything, maybe too much. The expected spectrum for 
    arbitrary velocity and the sensitivity of the experiment to arbitrary
    physics is here. But it also can load measured data, plot it against
    expectation, eventually do fitting etc.

    source_p: arguments to the source
    absorber_p: arguments to the absorber
    measurement_p: parameters for the detector, relative positions, 
                   measurement times, etc.
    """
    def __init__(self, source_p, absorber_p, measurement_p):
        # take in truth information of mossbauer setup
        # construct an expected pdf
        self.source = MossbauerSource(source_p)
        self.absorber = MossbauerAbsorber(absorber_p)
        self._setup_measurement(measurement_p)
        # init empty arrays for observed data
        self.clear_measurements()
        return

    def get_sensitivity(self, deltaE, model, **kwargs):
        """Median expected sensitivity 
        
        deltaE: minimum measureable energy shift for the experiment
                (assumed to be separation-independent)
        model: string identifier of the sensitivity model to use

        This will get more complicated when we think about separations.. really
        we should have some test statistic and the function should take in the
        radii being measured? Idk..
        """
        models = ['down_quark']
        assert model in models, "Model must be one of: " + str(models)
        rs = kwargs.get('separations', np.logspace(-9, -6, 100))
        sensi = getattr(self, f'_get_sensitivity_{model}')(deltaE, rs, **kwargs)
        return rs, sensi

    def info(self):
        """Placeholder... get all relevant info about the masurement"""
        return

    def set_velocity(self, vel):
        """Set the (relative?) velocity of the experiment
        For now this is a class attribute which can be
        an array or a number.
        """
        # TODO: generalize this to source and absorber velocity?
        self.velocity = vel
        return

    def transmitted_spectrum(self, vel):
        """Give the fraction of photons transmitted as a function of velocity

        Velocity can be an array or float. If array, you get a spectrum, and
        if float you get back a float. All returned values are in (0, 1).
        """
        return _integrate_function_inf(vel, self._transmission_integrand)

    def transmitted_spectrum_derivative(self, vel):
        return _integrate_function_inf(vel, self._transmission_derivative_integrand)

    def get_deltaEmin_linear(self, **kwargs):
        """First order expansion about velocity
        Seems to be within 0.1% of full calculation, and much
        faster.
        """
        # idk if confusing to let kwargs override these
        vels = kwargs.get('vels', self.source.linewidth*np.logspace(-6, 2, 10000)/2)
        acquisition_time = kwargs.get('acquisition_time', self.acquisition_time)

        rates = self.transmitted_spectrum(vels)
        ders = self.transmitted_spectrum_derivative(vels)
        min_dE = rate_to_deltaEmin(acquisition_time, rates, ders)

        slope_vel = vels[min_dE.argmin()]
        slope_rate = rates[min_dE.argmin()]
        return (slope_vel, slope_rate, min_dE.min())

    def get_deltaEmin_full(self, **kwargs):
        """Recursive calculation, should be fully correct"""
        # idk if confusing to let kwargs override these
        vels = kwargs.get('vels', self.source.linewidth*np.logspace(-6, 2, 10000))
        acquisition_time = kwargs.get('acquisition_time', self.acquisition_time)
        
        rates = self.transmitted_spectrum(vels)
        f = interp1d(rates, vels, fill_value='extrapolate')

        nnew = (acquisition_time*rates) + np.sqrt(acquisition_time * rates)
        vnew = f(nnew/acquisition_time) - vels

        slope_vel = vels[vnew.argmin()]
        slope_rate = rates[vnew.argmin()]
        return (slope_vel, slope_rate, vnew.min())

    #### I/O Stuff ####
    # Maybe this makes the class too big and ugly... I have no idea.
    def load_from_file(self, filename):
        """Grab actual spectrometer data from file"""
        df = pd.read_csv(filename, sep=r"\s+")
        df = df[list(name_map.values())]
        df = pd.concat(
            [
                df,
                pd.DataFrame({
                    file_name: getattr(self, my_name) for my_name, file_name in name_map.items()
                })
            ], 
            axis=0
        )
        df = df.groupby(df[name_map['measured_velocities']]).sum().reset_index()
        for my_name, file_name in name_map.items():
            setattr(self, my_name, df[file_name].values.tolist())
        self.measured_rates = np.asarray(self.measured_counts)/np.asarray(self.measured_times)
        self.measured_rate_errs = np.sqrt(np.asarray(self.measured_counts))/np.asarray(self.measured_times)
        return

    def clear_measurements(self):
        self.measured_velocities = []
        self.measured_counts = []
        self.measured_times = []
        return

    def plot(self, ax=None):
        if ax is None:
            fig = plt.figure()
            ax = fig.add_subplot(111)
        plt.errorbar(
            self.measured_velocities,
            self.measured_rates,
            yerr=self.measured_rate_errs,
            fmt='.',
            capsize=3,
        )

        plt.xlabel('velocity [mm/s]')
        plt.ylabel('rate [Hz]')
        plt.grid(alpha=0.3)

        ax2 = ax.twiny()
        velticks = ax.get_xticks()
        xrange = ax.get_xlim()
        Eticks = vel_to_E(velticks)
        values = Eticks
        plt.xticks(velticks, labels=Eticks)
        plt.xlim(xrange)
        plt.xlabel(r'$E$ [eV] - 14400')
        return

    # Hidden functions
    def _get_sensitivity_down_quark(self, deltaE, rs, **kwargs):
        """physics lives in separate module"""
        return physics.get_limits(rs, deltaE)

    def _setup_measurement(self, p):
        # duplicated code from MossbauerMaterial... idk.
        required_pars = [
            'acquisition_time',
            'solid_angle_fraction',
        ]
        check_pars(p, required_pars)
        self.__dict__.update(p)
        return

    def _transmission_integrand(self, E):
        # photons/s between E and E + dE
        transmission_integrand = (
            self.source.spectrum(E) 
            * self.solid_angle_fraction
            * np.exp(
                -1 
                * self.absorber.cross_section(E, self.velocity) 
                * self.absorber.thickness_normalized
            )
        )
        return transmission_integrand

    def _transmission_derivative_integrand(self, E):
        transmission_integrand = self._transmission_integrand(E)
        Ediff = E - (self.absorber.Eres - self.velocity)
        half_linewidth = self.absorber.linewidth/2.0
        extra_factor = (
            (half_linewidth**2.0)*Ediff 
            / (Ediff**2 + half_linewidth**2)**2
        )
        ## what is this 2 from?
        return 2 * transmission_integrand * extra_factor * self.absorber.thickness_normalized

    def _integrate_function_inf(self, vel, func):
        """Integrate function over (-inf, inf) numerically

        Velocity can be an array or float. If array, you get a spectrum, and
        if float you get back a float. All returned values are in (0, 1).
        """
        self.set_velocity(vel)
        args = [
            func,
            -np.inf,
            np.inf
        ]
        if hasattr(vel, '__len__'):
            return quad_vec(*args)[0]
        else:
            return quad(*args)[0]



if __name__=='__main__':
    natural_linewidth = 4.55e-9  # eV

    ### source parameters
    source_parameters = dict(
        Eres=0.0,
        linewidth=E_to_vel(natural_linewidth),
        total_activity=3.7e10 * 0.001  # 1 mCi
    )

    ### absorber parameters
    recoilless_fraction_A = 1.
    t_mgcm2 = 0.13  # potassium ferrocyanide from ritverc
    absorption_coefficient = 25.0  # cm^2 / mgFe57  (doublecheck)
    absorber_parameters = dict(
        Eres=0.,
        linewidth = E_to_vel(natural_linewidth),
        thickness_normalized=t_mgcm2 * absorption_coefficient * recoilless_fraction_A
    )

    ### measurement parameters
    detector_face_OD = 2 * 25.4  # mm
    detector_distance = 400.0  # mm
    measurement_parameters = dict(
        acquisition_time=3600.*24*31,
        solid_angle_fraction=(detector_face_OD**2)/(16*detector_distance**2),
    )

    moss = MossbauerMeasurement(
        source_parameters,
        absorber_parameters,
        measurement_parameters
    )

from ibllib.io import spikeglx
import numpy as np
import ibllib.dsp as dsp
from scipy import signal
from ibllib.misc import print_progress
from pathlib import Path
import alf.io as aio
import logging
import ibllib.ephys.ephysqc as ephysqc
from phylib.io import alf

_logger = logging.getLogger('ibllib')


RMS_WIN_LENGTH_SECS = 3
WELCH_WIN_LENGTH_SAMPLES = 1024


def rmsmap(fbin, spectra=True, max_length_in_sec=None):
    """
    Computes RMS map in time domain and spectra for each channel of Neuropixel probe

    :param fbin: binary file in spike glx format (will look for attached metatdata)
    :type fbin: str or pathlib.Path
    :param spectra: whether to compute the power spectrum (only need for lfp data)
    :type: bool
    :return: a dictionary with amplitudes in channeltime space, channelfrequency space, time
     and frequency scales
    """
    if not isinstance(fbin, spikeglx.Reader):
        sglx = spikeglx.Reader(fbin)
        sglx.open()
    rms_win_length_samples = 2 ** np.ceil(np.log2(sglx.fs * RMS_WIN_LENGTH_SECS))
    # the window generator will generates window indices
    wingen = dsp.WindowGenerator(ns=sglx.ns if max_length_in_sec is None else min(sglx.ns, max_length_in_sec * sglx.fs), 
                                 nswin=rms_win_length_samples, overlap=0)
    # pre-allocate output dictionary of numpy arrays
    win = {'TRMS': np.zeros((wingen.nwin, sglx.nc)),
           'nsamples': np.zeros((wingen.nwin,)),
           'fscale': dsp.fscale(WELCH_WIN_LENGTH_SAMPLES, 1 / sglx.fs, one_sided=True),
           'tscale': wingen.tscale(fs=sglx.fs)}
    win['spectral_density'] = np.zeros((len(win['fscale']), sglx.nc))
    # loop through the whole session
    for first, last in wingen.firstlast:
        D = sglx.read_samples(first_sample=first, last_sample=last)[0].transpose()
        # remove low frequency noise below 1 Hz
        D = dsp.hp(D, 1 / sglx.fs, [0, 1])
        iw = wingen.iw
        win['TRMS'][iw, :] = dsp.rms(D)
        win['nsamples'][iw] = D.shape[1]
        if spectra:
            # the last window may be smaller than what is needed for welch
            if last - first < WELCH_WIN_LENGTH_SAMPLES:
                continue
            # compute a smoothed spectrum using welch method
            _, w = signal.welch(D, fs=sglx.fs, window='hanning', nperseg=WELCH_WIN_LENGTH_SAMPLES,
                                detrend='constant', return_onesided=True, scaling='density',
                                axis=-1)
            win['spectral_density'] += w.T
        # print at least every 20 windows
        if (iw % min(20, max(int(np.floor(wingen.nwin / 75)), 1))) == 0:
            print_progress(iw, wingen.nwin)

    sglx.close()
    return win


def extract_rmsmap(fbin, out_folder=None, spectra=True, max_length_in_sec=None):
    """
    Wrapper for rmsmap that outputs _ibl_ephysRmsMap and _ibl_ephysSpectra ALF files

    :param fbin: binary file in spike glx format (will look for attached metatdata)
    :param out_folder: folder in which to store output ALF files. Default uses the folder in which
     the `fbin` file lives.
    :param spectra: whether to compute the power spectrum (only need for lfp data)
    :type: bool
    :return: None
    """
    _logger.info(f"Computing rmsmap for {fbin}")
    sglx = spikeglx.Reader(fbin)
    # check if output ALF files exist already:
    if out_folder is None:
        out_folder = Path(fbin).parent
    else:
        out_folder = Path(out_folder)
    alf_object_time = f'_iblqc_ephysTimeRms{sglx.type.upper()}'
    alf_object_freq = f'_iblqc_ephysSpectralDensity{sglx.type.upper()}'

    # crunch numbers
    rms = rmsmap(fbin, spectra=spectra, max_length_in_sec=max_length_in_sec)
    # output ALF files, single precision with the optional label as suffix before extension
    if not out_folder.exists():
        out_folder.mkdir()
    tdict = {'rms': rms['TRMS'].astype(np.single), 'timestamps': rms['tscale'].astype(np.single)}
    aio.save_object_npy(out_folder, object=alf_object_time, dico=tdict)
    if spectra:
        fdict = {'power': rms['spectral_density'].astype(np.single),
                 'freqs': rms['fscale'].astype(np.single)}
        aio.save_object_npy(out_folder, object=alf_object_freq, dico=fdict)
        
        
def extract_lfpcorr(lfp_file, out_folder=None, max_length_in_sec=None):
    """
    Extract lfp correlation and covariance matrix
    See https://github.com/hanhou/code_cache/tree/master/lfpSurface

    I bypassed the alf format here for my own convienence. -Han
    """
    _logger.info(f"Computing lfp correlation for {lfp_file}")
    if not isinstance(lfp_file, spikeglx.Reader):
        sglx = spikeglx.Reader(lfp_file)
        sglx.open()    
        
    # If max_length_in_sec is not None, use the last {max_length_in_sec} seconds of data
    start = 0 if max_length_in_sec is None else max(0, sglx.ns - int(max_length_in_sec * sglx.fs))
    lfp = sglx.read_samples(first_sample=start, last_sample=sglx.ns)[0].transpose()
    if max(lfp[-1, :]) > 1:  # If the LFP file has the sync channel, remove it
        lfp = lfp[:-1, :]
    
    # Corrcoef and covariance matrix
    lfp_corr = np.corrcoef(lfp)
    lfp_cov = np.cov(lfp)
    
    # Save data (not using alf format for simplicity)
    np.savez(out_folder.joinpath('lfp_corr'), lfp_corr=lfp_corr, lfp_cov=lfp_cov)    
        

def _sample2v(ap_file):
    """
    Convert raw ephys data to Volts
    """
    md = spikeglx.read_meta_data(ap_file.with_suffix('.meta'))
    s2v = spikeglx._conversion_sample2v_from_meta(md)
    return s2v['ap'][0]


def ks2_to_alf(ks_path, bin_path, out_path, bin_file=None, ampfactor=1, label=None, force=True):
    """
    Convert Kilosort 2 output to ALF dataset for single probe data
    :param ks_path:
    :param bin_path: path of raw data
    :param out_path:
    :return:
    """
    m = ephysqc.phy_model_from_ks2_path(ks2_path=ks_path, bin_path=bin_path, bin_file=bin_file)
    ephysqc.spike_sorting_metrics_ks2(ks_path, m, save=True, save_path=out_path)
    ac = alf.EphysAlfCreator(m)
    ac.convert(out_path, label=label, force=force, ampfactor=ampfactor)


def extract_data(ks_path, ephys_path, out_path, max_length_in_sec=None):
    efiles = spikeglx.glob_ephys_files(ephys_path)
    print(efiles)
    for efile in efiles:
        if efile.get('ap') and efile.ap.exists():
            ks2_to_alf(ks_path, ephys_path, out_path, bin_file=efile.ap,
                       ampfactor=_sample2v(efile.ap), label=None, force=True)

            extract_rmsmap(efile.ap, out_folder=out_path, spectra=False, max_length_in_sec=max_length_in_sec)
            pass
        if efile.get('lf') and efile.lf.exists():
            extract_lfpcorr(efile.lf, out_folder=out_path, max_length_in_sec=max_length_in_sec)
            extract_rmsmap(efile.lf, out_folder=out_path, max_length_in_sec=max_length_in_sec)
            pass


# if __name__ == '__main__':
#
#    ephys_path = Path('C:/Users/Mayo/Downloads/raw_ephys_data')
#    ks_path = Path('C:/Users/Mayo/Downloads/KS2')
#    out_path = Path('C:/Users/Mayo/Downloads/alf')
#    extract_data(ks_path, ephys_path, out_path)

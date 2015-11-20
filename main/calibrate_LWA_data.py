import datetime as DT
import numpy as NP
import matplotlib.pyplot as PLT
import matplotlib.colors as PLTC
import scipy.constants as FCNST
from astropy.io import fits
from astropy.io import ascii
from astropy.table import Table
import progressbar as PGB
import antenna_array as AA
import data_interface as DI
import geometry as GEOM
import sim_observe as SIM
import my_DSP_modules as DSP
import ipdb as PDB
import EPICal
import aperture as APR
import time

t1=time.time()

# Get file, read in header
infile = '/data3/t_nithyanandan/project_MOFF/data/samples/lwa_data.CDF.fits'
du = DI.DataHandler(indata=infile)
lat = du.latitude
f0 = du.center_freq
nts = du.nchan
nchan = nts * 2
fs = du.sample_rate
dt = 1/fs
freqs = du.freq
channel_width = du.freq_resolution
f_center = f0
bchan = 100
echan = 925
antid = du.antid
antpos = du.antpos
n_antennas = du.n_antennas
timestamps = du.timestamps
MOFF_tbinsize = None
n_timestamps = du.n_timestamps
npol = du.npol
ant_data = du.data

# Make some choices about the analysis
max_n_timestamps = 20
cal_iter=1
bchan = 200 # beginning channel (to cut out edges of the bandpass)
echan = 825 # ending channel
max_antenna_radius = 75.0 # meters. To cut outtrigger(s)


#### Antenna and array initialization

# Select antennas
core_ind = NP.logical_and((NP.abs(antpos[:,0]) < max_antenna_radius), (NP.abs(antpos[:,1]) < max_antenna_radius))
antid = antid[core_ind]
antpos = antpos[core_ind,:]
ant_info = NP.hstack((antid.reshape(-1,1),antpos))
n_antennas = ant_info.shape[0]
ant_data = ant_data[:,core_ind,:,:]

# Read in cable delays
stand_cable_delays = NP.loadtxt('/data3/t_nithyanandan/project_MOFF/data/samples/cable_delays.txt', skiprows=1)
antennas = stand_cable_delays[:,0].astype(NP.int).astype(str)
cable_delays = stand_cable_delays[:,1]

# Set up the beam
grid_map_method='sparse'
identical_antennas = True
ant_sizex = 3.0 # meters
ant_sizey = 3.0
ant_diameter = NP.sqrt(ant_sizex**2 + ant_sizey**2)
ant_kernshape = {pol: 'rect' for pol in ['P1','P2']}
ant_pol_type = 'dual'
ant_kerntype = {pol: 'func' for pol in ['P1','P2']}
ant_lookupinfo = None

ant_kernshapeparms = {pol: {'xmax':0.5*ant_sizex, 'ymax':0.5*ant_sizey, 'rmin': 0.0, 'rmax': 0.5*ant_diameter, 'rotangle':0.0} for pol in ['P1','P2']}
ant_aprtr = APR.Aperture(pol_type=ant_pol_type, kernel_type=ant_kerntype,
                         shape=ant_kernshape, parms=ant_kernshapeparms,
                         lkpinfo=ant_lookupinfo, load_lookup=True)
if identical_antennas:
    ant_aprtrs = [ant_aprtr] * n_antennas

# Set up antenna array
ants = []
aar = AA.AntennaArray()
for i in xrange(n_antennas):
    ant = AA.Antenna('{0:0d}'.format(int(ant_info[i,0])),lat,ant_info[i,1:],f0, nsamples=nts, aperture=ant_aprtrs[i])
    ant.f = ant.f0 + DSP.spectax(2*nts, dt, shift=True)
    ants += [ant]
    aar = aar + ant

aar.grid(uvspacing=0.25, xypad=2*NP.max([ant_sizex,ant_sizey]))
antpos_info = aar.antenna_positions(sort=True, centering=True)

# Select time steps
if max_n_timestamps is None:
    max_n_timestamps = len(timestamps)
else:
    max_n_timestamps = min(max_n_timestamps, len(timestamps))

timestamps = timestamps[:max_n_timestamps]

#### Set up sky model

n_src = 2 # Just Cyg A and Cas A for now
skypos=NP.array([[0.007725,0.116067],[0.40582995,0.528184]])
src_flux = NP.array([16611.68,17693.9])
src_flux[1] = src_flux[1] * 0.57 # Manually adjusting by a rough factor of the beam because the cal module doesn't do it (yet)
nvect = NP.sqrt(1.0-NP.sum(skypos**2, axis=1)).reshape(-1,1) 
skypos = NP.hstack((skypos,nvect))

sky_model = NP.zeros((n_src,nchan,4))
sky_model[:,:,0:3] = skypos.reshape(n_src,1,3)
sky_model[:,:,3] = src_flux.reshape(n_src,1)

####  set up calibration
calarr={}
cal_fi=arange(0.0,nchan/2,0.5)
fi=arange(freqs.shape[0])
cal_freqs = NP.interp(cal_fi,fi,freqs)
# auto noise term
auto_noise_model = 0.25 * NP.sum(sky_model[:,0,3]) # roughly rxr:sky based on Ellingson, 2013
#auto_noise_model=0.0
for pol in ['P1','P2']:
    calarr[pol] = EPICal.cal(cal_freqs,antpos,pol=pol,sim_mode=False,n_iter=cal_iter,damping_factor=0.75,inv_gains=False,sky_model=sky_model,freq_ave=bchan,exclude_autos=True,phase_fit=False)

# Create array of gains to watch them change
ncal=max_n_timestamps/cal_iter
cali=0
gain_stack = NP.zeros((ncal+1,ant_info.shape[0],nchan),dtype=NP.complex64)
amp_stack = NP.zeros((ncal+1,nchan),dtype=NP.float64)
amp_full_stack = NP.zeros((max_n_timestamps,nchan),dtype=NP.float64)
temp_amp = NP.zeros(nchan,dtype=NP.float64)

for i in xrange(max_n_timestamps):
    print i

    timestamp = timestamps[i]
    update_info={}
    update_info['antennas']=[]
    update_info['antenna_array']={}
    update_info['antenna_array']['timestamp']=timestamp

    print 'Consolidating Antenna updates...'
    progress = PGB.ProgressBar(widgets=[PGB.Percentage(), PGB.Bar(marker='-', left=' |', right='| '), PGB.Counter(), '/{0:0d} Antennas '.format(n_antennas), PGB.ETA()], maxval=n_antennas).start()
    antnum = 0
    for ia, label in enumerate(antid):
        adict={}
        adict['label']=label
        adict['action']='modify'
        adict['timestamp']=timestamp
        adict['t'] = NP.arange(nts) * dt
        adict['gridfunc_freq'] = 'scale'
        adict['gridmethod'] = 'NN'
        adict['distNN'] = 3.0
        adict['tol'] = 1.0e-6
        adict['maxmatch'] = 1
        adict['Et'] = {}
        adict['flags'] = {}
        adict['stack'] = True
        adict['wtsinfo'] = {}
        adict['delaydict'] = {}
        for ip,pol in enumerate(['P1','P2']):
            adict['flags'][pol] = False
            adict['delaydict'][pol] = {}
            adict['delaydict'][pol]['frequencies'] = freqs
            adict['delaydict'][pol]['delays'] = cable_delays[antennas == label]
            adict['delaydict'][pol]['fftshifted'] = True
            adict['wtsinfo'][pol] = [{'orientation':0.0, 'lookup':'/data3/t_nithyanandan/project_MOFF/simulated/LWA/data/lookup/E_illumination_isotropic_radiators_lookup_zenith.txt'}]
            adict['Et'][pol] = ant_data[i,ia,:,ip]
            if NP.any(NP.isnan(adict['Et'][pol])):
                adict['flags'][pol] = True
            else:
                adict['flags'][pol] = False
            
        update_info['antennas'] += [adict]

        progress.update(antnum+1)
        antnum += 1
    progress.finish()

    aar.update(update_info, parallel=False, verbose=False)

    ### Calibration steps
    for pol in ['P1','P2']:
        # read in data array
        aar.caldata[pol]=aar.get_E_fields(pol,sort=True)
        tempdata=aar.caldata[pol]['E-fields'][0,:,:].copy()
        # Apply calibration and put back into antenna array
        #aar.caldata[pol]['E-fields'][0,:,:]=NP.ones((n_antennas,nchan),NP.complex64)
        aar.caldata[pol]['E-fields'][0,:,:]=calarr[pol].apply_cal(tempdata)

    if grid_map_method == 'regular':
        aar.grid_convolve_new(pol='P1', method='NN', distNN=0.5*NP.sqrt(ant_sizex**2+ant_sizey**2), identical_antennas=False, cal_loop=False, gridfunc_freq='scale', wts_change=False, parallel=False, pp_method='pool')    
    else:
        if i == 0:
            aar.genMappingMatrix(pol='P1', method='NN', distNN=0.5*NP.sqrt(ant_sizex**2+ant_sizey**2), identical_antennas=True, gridfunc_freq='scale', wts_change=False, parallel=False)

    if i == 0:
        imgobj = AA.NewImage(antenna_array=aar, pol='P1')
    else:
        imgobj.update(antenna_array=aar, reset=True)
    
    imgobj.imagr(weighting='natural',pol='P1',pad=0,verbose=False,grid_map_method=grid_map_method,cal_loop=True,stack=False)

    # update calibration
    calarr['P1'].update_cal(tempdata,imgobj)

    if i == 0:
        avg_img = imgobj.img['P1'].copy()
        im_stack = NP.zeros((ncal,avg_img.shape[0],avg_img.shape[1]),dtype=NP.double)
        im_stack[cali,:,:] = NP.mean(avg_img[:,:,bchan:echan].copy(),axis=2)
        temp_im = avg_img[:,:,bchan+1]

        temp_amp = NP.abs(tempdata[0,:])**2
        gain_stack[cali,:,:] = calarr['P1'].curr_gains
        amp_stack[cali,:] = NP.abs(tempdata[0,:])**2
        cali += 1
        gain_stack[cali,:,:] = calarr['P1'].curr_gains

    else:
        avg_img = avg_img+imgobj.img['P1'].copy()
        temp_im = temp_im+NP.mean(imgobj.img['P1'][:,:,bchan:echan].copy(),axis=2)

        temp_amp += NP.abs(tempdata[0,:])**2
        if i % cal_iter == 0:
            im_stack[cali,:,:] = temp_im/cal_iter
            temp_im[:] = 0.0
            gain_stack[cali,:,:] = calarr['P1'].curr_gains
            amp_stack[cali,:] = temp_amp/cal_iter
            temp_amp[:] = 0.0
            cali += 1



    if True in NP.isnan(calarr['P1'].cal_corr):
    #if True in NP.isnan(calarr['P1'].temp_gains):
        print 'NAN in calibration gains! exiting!'
        break

    avg_img /= max_n_timestamps

imgobj.accumulate(tbinsize=MOFF_tbinsize)
imgobj.removeAutoCorr(forceeval=True, datapool='avg', pad=0)
t2=time.time()

print 'Full loop took ', t2-t1, 'seconds'
#    PDB.set_trace()

### Do some plotting

f_images = PLT.figure("Images",figsize=(15,5))
ax1 = PLT.subplot(121)
imshow(im_stack[1,:,:],aspect='equal',origin='lower',extent=(imgobj.gridl.min(),imgobj.gridl.max(),imgobj.gridm.min(),imgobj.gridm.max()),interpolation='none')
xlim([-1.0,1.0])
ylim([-1.0,1.0])
ax2 = PLT.subplot(122)
imshow(im_stack[-2,:,:],aspect='equal',origin='lower',extent=(imgobj.gridl.min(),imgobj.gridl.max(),imgobj.gridm.min(),imgobj.gridm.max()),interpolation='none')
plot(sky_model[:,0,0],sky_model[:,0,1],'o',mfc='none',mec='red',mew=1,ms=10)
xlim([-1.0,1.0])
ylim([-1.0,1.0])

# remove some arbitrary phases.
#data = gain_stack[1:-1,:,bchan+1]*calarr['P1'].sim_gains[calarr['P1'].ref_ant,2]*NP.conj(gain_stack[-2,calarr['P1'].ref_ant,2])/NP.abs(calarr['P1'].sim_gains[calarr['P1'].ref_ant,2]*gain_stack[-2,calarr['P1'].ref_ant,2])
#true_g = calarr['P1'].sim_gains[:,2]

# Phase and amplitude convergence
#f_phases = PLT.figure("Phases")
#f_amps = PLT.figure("Amplitudes")
#for i in xrange(gain_stack.shape[1]):
#    PLT.figure(f_phases.number)
#    plot(NP.angle(data[:,i]*NP.conj(true_g[i])))
#    PLT.figure(f_amps.number)
#    plot(NP.abs(data[:,i]/true_g[i]))

# Histogram
#f_hist = PLT.figure("Histogram")
#PLT.hist(NP.real(data[-1,:]-true_g),histtype='step')
#PLT.hist(NP.imag(data[-1,:]-true_g),histtype='step')

# Expected noise
#Nmeas_eff = itr
#Nmeas_eff = 100
#Nmeas_eff = cal_iter / (1-calarr['P1'].gain_factor)
#visvar = NP.sum(sky_model[:,2,3])**2 / Nmeas_eff
#gvar = 4 * visvar / (NP.sum(abs(true_g.reshape(1,calarr['P1'].n_ant) * calarr['P1'].model_vis[:,:,2])**2,axis=1) - NP.abs(true_g * NP.diag(calarr['P1'].model_vis[:,:,2])))




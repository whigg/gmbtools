#! /usr/bin/env python
"""
Compute dh/dt and mass balance for input DEMs and glacier polygons
"""

"""
Todo:
Curves for PRISM T an precip vs. mb
Filling using dz/dt obs
Add date fields to mb curve output
Write z1, z2, dz, stats etc to GlacFeat object
Export polygons with mb numbers as geojson, spatialite, shp?
Add +/- std for each dh/dt polygon, some idea of spread
Clean up mb_proc function, one return, globals
Should move everything to main, pass args to mb_proc
CONUS z1_date update in mb_proc
Better penetration correction
Filling gaps
Error estimates
"""

import sys
import os
import subprocess
from datetime import datetime, timedelta
import time
import pickle

import numpy as np
import matplotlib.pyplot as plt
from osgeo import gdal, ogr

from pygeotools.lib import malib
from pygeotools.lib import warplib
from pygeotools.lib import geolib
from pygeotools.lib import iolib
from pygeotools.lib import timelib

from imview.lib import pltlib

"""
Class to store relevant feature attributes and derived values
Safe for multiprocessing
"""
class GlacFeat:
    def __init__(self, feat, glacname_fieldname, glacnum_fieldname):

        self.glacname = feat.GetField(glacname_fieldname)
        if self.glacname is None:
            self.glacname = ""
        else:
            #RGI has some nonstandard characters
            self.glacname = self.glacname.decode('unicode_escape').encode('ascii','ignore')
            self.glacname = self.glacname.replace(" ", "")
            self.glacname = self.glacname.replace("_", "")

        self.glacnum = feat.GetField(glacnum_fieldname)
        fn = feat.GetDefnRef().GetName()
        if '24k' in fn:
            self.glacnum = int(self.glacnum)
        else:
            #RGIId (String) = RGI50-01.00004
            self.glacnum = '%0.5f' % float(self.glacnum.split('-')[-1])

        if self.glacname:
            self.feat_fn = "%s_%s" % (self.glacnum, self.glacname)
        else:
            self.feat_fn = str(self.glacnum)

        self.glac_geom_orig = geolib.geom_dup(feat.GetGeometryRef())
        self.glac_geom = geolib.geom_dup(self.glac_geom_orig)

        #Attributes written by mb_calc
        self.z1 = None
        self.z1_stats = None
        self.z1_ela = None
        self.z2 = None
        self.z2_stats = None
        self.z2_ela = None
        self.z2_aspect = None
        self.z2_aspect_stats = None
        self.z2_slope = None
        self.z2_slope_stats = None
        self.res = None
        self.dhdt = None
        self.mb = None
        self.mb_mean = None
        self.t1 = None
        self.t2 = None
        self.dt = None

    def geom_attributes(self, srs=None):
        if srs is not None:
            #Should reproject here to equal area, before geom_attributes
            #self.glac_geom.AssignSpatialReference(glac_shp_srs)
            #self.glac_geom_local = geolib.geom2localortho(self.glac_geom)
            geolib.geom_transform(self.glac_geom, srs)

        self.glac_geom_extent = geolib.geom_extent(self.glac_geom)
        self.glac_area = self.glac_geom.GetArea()
        self.cx, self.cy = self.glac_geom.Centroid().GetPoint_2D()

def srtm_corr(z1):
    #Should separate into different regions from Kaab et al (2012)
    #Should separate into firn/snow, clean ice, and debris-covered ice
    #See Gardelle et al (2013) for updated numbers
    #Integrate Batu's debris-cover maps or Kaab LS classification?
    #Snowcover in Feb 2000 from MODSCAG:
    #/nobackup/deshean/data/srtm_corr/20000224_snow_fraction_20000309_snow_fraction_stack_15_med.tif

    #For now, use Kaab et al (2012) region-wide mean of 2.1 +/- 0.4
    offset = 2.1

    return z1 + offset

def z_vs_dz(z,dz):
    plt.scatter(z.compressed(), dz.compressed())

def get_bins(dem, bin_width=100.0):
    #Define min and max elevation
    minz, maxz= list(malib.calcperc(dem, perc=(0.01, 99.99)))
    minz = np.floor(minz/bin_width) * bin_width
    maxz = np.ceil(maxz/bin_width) * bin_width
    #Compute bin edges and centers
    bin_edges = np.arange(minz, maxz + bin_width, bin_width)
    bin_centers = bin_edges[:-1] + np.diff(bin_edges)/2.0
    return bin_edges, bin_centers

#RGI uses 50 m bins
def hist_plot(gf, outdir, bin_width=10.0):
    #print("Generating histograms")
    z_bin_edges, z_bin_centers = get_bins(gf.z1, bin_width)
    z1_bin_counts, z1_bin_edges = np.histogram(gf.z1, bins=z_bin_edges)
    z1_bin_areas = z1_bin_counts * gf.res[0] * gf.res[1] / 1E6
    #RGI standard is integer thousandths of glaciers total area
    #Should check to make sure sum of bin areas equals total area
    z1_bin_areas_perc = 100. * z1_bin_areas / np.sum(z1_bin_areas)
    z2_bin_counts, z2_bin_edges = np.histogram(gf.z2, bins=z_bin_edges)
    z2_bin_areas = z2_bin_counts * gf.res[0] * gf.res[1] / 1E6
    z2_bin_areas_perc = 100. * z2_bin_areas / np.sum(z2_bin_areas)

    #dz_bin_edges, dz_bin_centers = get_bins(dz, 1.)
    #dz_bin_counts, dz_bin_edges = np.histogram(dz, bins=dz_bin_edges)
    #dz_bin_areas = dz_bin_counts * res * res / 1E6
    mb_bin_med = np.ma.masked_all_like(z1_bin_areas)
    mb_bin_mad = np.ma.masked_all_like(z1_bin_areas)
    dz_bin_med = np.ma.masked_all_like(z1_bin_areas)
    dz_bin_mad = np.ma.masked_all_like(z1_bin_areas)
    idx = np.digitize(gf.z1, z_bin_edges)
    for bin_n in range(z_bin_centers.size):
        mb_bin_samp = gf.mb[(idx == bin_n+1)]
        if mb_bin_samp.count() > 0:
            mb_bin_med[bin_n] = malib.fast_median(mb_bin_samp)
            mb_bin_mad[bin_n] = malib.mad(mb_bin_samp)
            mb_bin_med[bin_n] = mb_bin_samp.mean()
            mb_bin_mad[bin_n] = mb_bin_samp.std()
        dz_bin_samp = gf.dhdt[(idx == bin_n+1)]
        if dz_bin_samp.count() > 0:
            dz_bin_med[bin_n] = malib.fast_median(dz_bin_samp)
            dz_bin_mad[bin_n] = malib.mad(dz_bin_samp)
            dz_bin_med[bin_n] = dz_bin_samp.mean()
            dz_bin_mad[bin_n] = dz_bin_samp.std()

    #Should also export original dh/dt numbers, not mb
    #outbins_header = 'bin_center_elev, bin_count, dhdt_bin_med, dhdt_bin_mad, mb_bin_med, mb_bin_mad'
    #outbins = np.ma.dstack([z_bin_centers, z1_bin_counts, dz_bin_med, dz_bin_mad, mb_bin_med, mb_bin_mad]).astype('float32')[0]
    #fmt='%0.2f'
    outbins_header = 'bin_center_elev_m, z1_bin_count_valid, z1_bin_area_valid_km2, z1_bin_area_perc, z2_bin_count_valid, z2_bin_area_valid_km2, z2_bin_area_perc, dhdt_bin_med_ma, dhdt_bin_mad_ma, mb_bin_med_mwe, mb_bin_mad_mwe'
    fmt='%0.1f, %i, %0.3f, %0.2f, %i, %0.3f, %0.2f, %0.2f, %0.2f, %0.2f, %0.2f' 
    outbins = np.ma.dstack([z_bin_centers, z1_bin_counts, z1_bin_areas, z1_bin_areas_perc, z2_bin_counts, z2_bin_areas, z2_bin_areas_perc, dz_bin_med, dz_bin_mad, mb_bin_med, mb_bin_mad]).astype('float32')[0]
    np.ma.set_fill_value(outbins, -9999.0)
    outbins_fn = os.path.join(outdir, gf.feat_fn+'_mb_bins.csv')
    np.savetxt(outbins_fn, outbins, fmt=fmt, delimiter=',', header=outbins_header)

    #print("Generating aed plot")
    f,axa = plt.subplots(1,2, figsize=(6, 6))
    f.suptitle(gf.feat_fn)
    axa[0].plot(z1_bin_areas, z_bin_centers, label='%0.2f' % gf.t1)
    axa[0].plot(z2_bin_areas, z_bin_centers, label='%0.2f' % gf.t2)
    axa[0].axhline(gf.z1_ela, ls=':', c='C0')
    axa[0].axhline(gf.z2_ela, ls=':', c='C1')
    axa[0].legend(prop={'size':8}, loc='upper right')
    axa[0].set_ylabel('Elevation (m WGS84)')
    axa[0].set_xlabel('Area $\mathregular{km^2}$')
    axa[0].minorticks_on()
    axa[1].yaxis.tick_right()
    axa[1].yaxis.set_label_position("right")
    axa[1].axvline(0, lw=1.0, c='k')
    axa[1].axvline(gf.mb_mean, lw=0.5, ls=':', c='k', label='%0.2f m w.e./yr' % gf.mb_mean)
    axa[1].legend(prop={'size':8}, loc='upper right')
    axa[1].plot(mb_bin_med, z_bin_centers, color='k')
    axa[1].fill_betweenx(z_bin_centers, 0, mb_bin_med, where=(mb_bin_med<0), color='r', alpha=0.2)
    axa[1].fill_betweenx(z_bin_centers, 0, mb_bin_med, where=(mb_bin_med>0), color='b', alpha=0.2)
    #axa[1].set_ylabel('Elevation (m WGS84)')
    #axa[1].set_xlabel('dh/dt (m/yr)')
    axa[1].set_xlabel('mb (m w.e./yr)')
    axa[1].minorticks_on()
    axa[1].set_xlim(-2.0, 2.0)
    #axa[1].set_xlim(-8.0, 8.0)
    plt.tight_layout()
    #Make room for suptitle
    plt.subplots_adjust(top=0.95)
    #print("Saving aed plot")
    fig_fn = os.path.join(outdir, gf.feat_fn+'_mb_aed.png')
    plt.savefig(fig_fn, dpi=300)
    return z_bin_edges

def map_plot(gf, z_bin_edges, outdir):
    #print("Generating map plot")
    f,axa = plt.subplots(1,3, figsize=(10,7.5))
    f.suptitle(gf.feat_fn)
    alpha = 1.0
    hs = True
    if hs:
        z1_hs = geolib.gdaldem_wrapper(gf.out_z1_fn, product='hs', returnma=True, verbose=False)
        z2_hs = geolib.gdaldem_wrapper(gf.out_z2_fn, product='hs', returnma=True, verbose=False)
        hs_clim = malib.calcperc(z2_hs, (2,98))
        z1_hs_im = axa[0].imshow(z1_hs, cmap='gray', vmin=hs_clim[0], vmax=hs_clim[1])
        z2_hs_im = axa[1].imshow(z2_hs, cmap='gray', vmin=hs_clim[0], vmax=hs_clim[1])
        alpha = 0.5
    z1_im = axa[0].imshow(gf.z1, cmap='cpt_rainbow', vmin=z_bin_edges[0], vmax=z_bin_edges[-1], alpha=alpha)
    z2_im = axa[1].imshow(gf.z2, cmap='cpt_rainbow', vmin=z_bin_edges[0], vmax=z_bin_edges[-1], alpha=alpha)
    axa[0].contour(gf.z1, [gf.z1_ela,], linewidths=0.5, linestyles=':', colors='w')
    axa[1].contour(gf.z2, [gf.z2_ela,], linewidths=0.5, linestyles=':', colors='w')
    #t1_title = int(np.round(gf.t1))
    #t2_title = int(np.round(gf.t2))
    t1_title = int(gf.t1)
    t2_title = int(gf.t2)
    #t1_title = gf.t1.strftime('%Y-%m-%d')
    #t2_title = gf.t2.strftime('%Y-%m-%d')
    axa[0].set_title(t1_title)
    axa[1].set_title(t2_title)
    axa[2].set_title('%s to %s (%0.2f yr)' % (t1_title, t2_title, gf.dt))
    #dz_clim = (-10, 10)
    dz_clim = (-2.0, 2.0)
    dz_im = axa[2].imshow(gf.dhdt, cmap='RdBu', vmin=dz_clim[0], vmax=dz_clim[1])
    for ax in axa:
        pltlib.hide_ticks(ax)
        ax.set_facecolor('k')
    sb_loc = pltlib.best_scalebar_location(gf.z1)
    pltlib.add_scalebar(axa[0], gf.res[0], location=sb_loc)
    pltlib.add_cbar(axa[0], z1_im, label='Elevation (m WGS84)')
    pltlib.add_cbar(axa[1], z2_im, label='Elevation (m WGS84)')
    pltlib.add_cbar(axa[2], dz_im, label='dh/dt (m/yr)')
    plt.tight_layout()
    #Make room for suptitle
    plt.subplots_adjust(top=0.90)
    #print("Saving map plot")
    fig_fn = os.path.join(outdir, gf.feat_fn+'_mb_map.png')
    plt.savefig(fig_fn, dpi=300)
    
topdir='/nobackup/deshean'
site='conus'
#site='hma'

#This was for focused mb at specific sites
#topdir='/Volumes/SHEAN_1TB_SSD/site_poly_highcount_rect3_rerun/rainier'
#site='rainier'
#topdir='/Volumes/SHEAN_1TB_SSD/site_poly_highcount_rect3_rerun/scg'
#topdir='.'
#site='other'

#Filter glacier poly - let's stick with big glaciers for now
min_glac_area = 0.1 #km^2
#Minimum percentage of glacier poly covered by valid dz
min_valid_area_perc = 0.80
#Write out DEMs and dz map
writeout = True 
#Generate figures
mb_plot = True 
#Only write out for larger glaciers
min_glac_area_writeout = 1.0
#Run in parallel, set to False for serial loop
parallel = True 
#Verbose for debugging
verbose = False 
#Number of parallel processes
nproc = iolib.cpu_count() - 1
#This stores collection of feature geometries, independent of shapefile
glacfeat_fn = "glacfeat_list.p"

"""
#Store setup variables in dictionary that can be passed to Process
setup = {}
setup['site'] = site
"""

if site == 'conus':
    #Glacier shp
    #glac_shp_fn = os.path.join(topdir,'data/rgi60/regions/rgi60_merge.shp')
    #ogr2ogr -t_srs '+proj=aea +lat_1=36 +lat_2=49 +lat_0=43 +lon_0=-115 +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs ' 24k_selection_aea.shp 24k_selection_32610.shp
    #glac_shp_fn = '/nobackupp8/deshean/conus/shp/24k_selection_aea.shp'
    #This has already been filtered by area
    #Note: SQL queries don't like the layer name with numbers and periods
    #glac_shp_fn = os.path.join(topdir,'conus/shp/24k_selection_aea_min0.1km2.shp')
    #glac_shp_fn = os.path.join(topdir,'data/rgi60/regions/rgi60_merge_CONUS.geojson')
    #glac_shp_fn = os.path.join(topdir,'data/rgi60/regions/rgi60_merge_CONUS.shp')
    glac_shp_fn = os.path.join(topdir,'data/rgi60/regions/rgi60_merge_CONUS_aea.shp')
    #This stores collection of feature geometries, independent of shapefile
    glacfeat_fn = os.path.splitext(glac_shp_fn)[0]+'_glacfeat_list.p'

    #First DEM source
    z1_fn = os.path.join(topdir,'rpcdem/ned1_2003/ned1_2003_adj.vrt')
    #NED 2003 dates
    z1_date_shp_fn = os.path.join(topdir,'rpcdem/ned1_2003/meta0306_PAL_24k_10kmbuffer_clean_dissolve_aea.shp')
    #ogr2ogr -t_srs '+proj=aea +lat_1=36 +lat_2=49 +lat_0=43 +lon_0=-115 +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs ' meta0306_PAL_24k_10kmbuffer_clean_dissolve_aea.shp meta0306_PAL_24k_10kmbuffer_clean_dissolve_32611.shp
    z1_date_shp_ds = ogr.Open(z1_date_shp_fn)
    z1_date_shp_lyr = z1_date_shp_ds.GetLayer()
    z1_date_shp_srs = z1_date_shp_lyr.GetSpatialRef()
    z1_date_shp_lyr.ResetReading()
    z1_sigma = 4.0

    srtm_penetration_corr = False

    #Second DEM source (WV mosaic)
    mosdir = 'conus_combined/mos/conus_20171018_mos/all/mos_8m_trans'
    #mosdir = 'conus_combined/mos/conus_20171017_mos/summer/mos_8m_trans'
    #z2_fn = os.path.join(topdir,'conus/dem2/conus_8m_tile_coreg_round3_summer2014-2016/conus_8m_tile_coreg_round3_summer2014-2016.vrt')
    #z2_fn = os.path.join(topdir,'conus_combined/mos/%s/mos_8m/%s_8m.vrt' % (mosdir, mosdir))
    #z2_fn = os.path.join(topdir, mosdir+'/conus_20171018_mos_8m_trans.vrt')
    z2_fn = os.path.join(topdir, mosdir+'/conus_20171017_mos_8m.vrt')
    z2_date = datetime(2015, 1, 1)
    z2_date = 2015.0
    z2_sigma = 1.0

    #Output directory
    outdir = os.path.join(topdir,'%s/mb' % mosdir)

    #Output projection
    #'+proj=aea +lat_1=36 +lat_2=49 +lat_0=43 +lon_0=-115 +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs '
    aea_srs = geolib.conus_aea_srs

    #PRISM climate data, 800-m 
    prism_ppt_annual_fn = os.path.join(topdir,'conus/prism/normals/annual/ppt/PRISM_ppt_30yr_normal_800mM2_annual_bil.bil')
    prism_tmean_annual_fn = os.path.join(topdir,'conus/prism/normals/annual/tmean/PRISM_tmean_30yr_normal_800mM2_annual_bil.bil')
    prism_ppt_summer_fn = os.path.join(topdir,'conus/prism/normals/monthly/PRISM_ppt_30yr_normal_800mM2_06-09_summer_mean.tif')
    prism_ppt_winter_fn = os.path.join(topdir,'conus/prism/normals/monthly/PRISM_ppt_30yr_normal_800mM2_10-05_winter_mean.tif')
    prism_tmean_summer_fn = os.path.join(topdir,'conus/prism/normals/monthly/PRISM_tmean_30yr_normal_800mM2_06-09_summer_mean.tif')
    prism_tmean_winter_fn = os.path.join(topdir,'conus/prism/normals/monthly/PRISM_tmean_30yr_normal_800mM2_10-05_winter_mean.tif')

    #Define priority glaciers 
    glacier_dict = {}
    glacier_dict[6012] = 'EmmonsGlacier'
    glacier_dict[6096] = 'Nisqually-WilsonGlacier'
    glacier_dict[10480] = 'SouthCascadeGlacier'
    #Note: Sandalee has 3 records, 2693, 2695, 2696
    glacier_dict[2696] = 'SandaleeGlacier'
    glacier_dict[3070] = 'NorthKlawattiGlacier'
    glacier_dict[1969] = 'NoisyCreekGlacier'
    glacier_dict[2294] = 'SilverGlacier'
    glacier_dict[2500] = 'EastonGlacier'
    glacier_dict[5510] = 'BlueGlacier'
    glacier_dict[5603] = 'EelGlacier'
    glacier_dict[1589] = 'SperryGlacier'
    glacier_dict[1277] = 'GrinnellGlacier'
    glacier_dict[10490] = 'ConnessGlacier'
    glacier_dict[10071] = 'WheelerGlacier'
    glacier_dict[9129] = 'LyellGlacier'
    glacier_dict[9130] = 'LyellGlacier'

elif site == 'hma':
    #glac_shp_fn = os.path.join(topdir,'data/rgi50/regions/rgi50_hma_aea.shp')
    #glac_shp_fn = os.path.join(topdir,'data/rgi60/regions/rgi60_merge_HMA.geojson')
    glac_shp_fn = os.path.join(topdir,'data/rgi60/regions/rgi60_merge_HMA_aea.shp')
    glacfeat_fn = os.path.splitext(glac_shp_fn)[0]+'_glacfeat_list.p'

    #First DEM source
    #SRTM
    z1_fn = os.path.join(topdir,'rpcdem/hma/srtm1/hma_srtm_gl1.vrt')
    #z1_date = timelib.dt2decyear(datetime(2000,2,11))
    z1_date = datetime(2000,2,11)
    z1_sigma = 4.0

    srtm_penetration_corr = True

    mosdir = 'hma_20170716_mos'
    #Second DEM Source (WV mosaic)
    #z2_fn = '/nobackup/deshean/hma/hma1_2016dec22/hma_8m_tile/hma_8m.vrt'
    #z2_fn = os.path.join(topdir,'hma/hma1_2016dec22/hma_8m_tile/hma_8m.vrt')
    #z2_fn = os.path.join(topdir,'hma/hma1_2016dec22/hma_8m_tile_round2_20170220/hma_8m_round2.vrt')
    #z2_fn = os.path.join(topdir,'hma/hma_8m_mos_20170410/hma_8m.vrt')
    z2_fn = os.path.join(topdir,'hma/mos/%s/mos_8m/%s_8m.vrt' % (mosdir, mosdir))
    #z2_date = 2015.0
    z2_date = datetime(2015, 1, 1)
    z2_sigma = 1.0

    #Output directory
    outdir = os.path.join(topdir,'hma/mos/%s/mb' % mosdir)

    #Output projection
    #'+proj=aea +lat_1=25 +lat_2=47 +lat_0=36 +lon_0=85 +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs '
    aea_srs = geolib.hma_aea_srs

elif site == 'other':
    outdir = os.path.join(topdir,'mb')
    aea_srs = geolib.conus_aea_srs
    #Can specify custom subset of glacier polygons
    #glac_shp_fn = '/Users/dshean/data/conus_glacierpoly_24k/rainier_24k_1970-2015_mb_aea.shp'
    #glac_shp_fn = '/Users/dshean/data/conus_glacierpoly_24k/conus_glacierpoly_24k_aea.shp'
    #glac_shp_fn = '/Users/dshean/data/conus_glacierpoly_24k/conus_glacierpoly_24k_32610_scg_2008_aea.shp'
    glac_shp_fn = os.path.join(topdir,'data/rgi60/regions/rgi60_merge.shp')
    z1_fn = sys.argv[1]
    z1_date = timelib.mean_date(timelib.fn_getdatetime_list(z1_fn))
    z2_fn = sys.argv[2]
    z2_date = timelib.mean_date(timelib.fn_getdatetime_list(z2_fn))
else:
    sys.exit()

ts = datetime.now().strftime('%Y%m%d_%H%M')
out_fn = '%s_mb_%s.csv' % (site, ts)
out_fn = os.path.join(outdir, out_fn)

#Write out temporary file line by line, incase processes interrupted
#import csv
#f = open(os.path.splitext(out_fn)[0]+'_temp.csv', 'wb')
#writer = csv.writer(f)

#List to hold output
out = []

if '24k' in glac_shp_fn: 
    glacname_fieldname = "GLACNAME"
    glacnum_fieldname = "GLACNUM"
    glacnum_fmt = '%i'
elif 'rgi' in glac_shp_fn:
    #Use RGI
    glacname_fieldname = "Name"
    #RGIId (String) = RGI50-01.00004
    glacnum_fieldname = "RGIId"
    glacnum_fmt = '%08.5f'
else:
    sys.exit('Unrecognized glacier shp filename')

glac_shp_ds = ogr.Open(glac_shp_fn, 0)
glac_shp_lyr = glac_shp_ds.GetLayer()
#This should be contained in features
glac_shp_srs = glac_shp_lyr.GetSpatialRef()
feat_count = glac_shp_lyr.GetFeatureCount()
print("Input glacier polygon count: %i" % feat_count)

z1_ds = gdal.Open(z1_fn)
z2_ds = gdal.Open(z2_fn)
dz_int_geom = geolib.ds_geom_intersection([z1_ds, z2_ds], t_srs=glac_shp_srs)

#Spatial filter
glac_shp_lyr.SetSpatialFilter(dz_int_geom)
feat_count = glac_shp_lyr.GetFeatureCount()
print("Glacier polygon count after spatial filter: %i" % feat_count)
glac_shp_lyr.ResetReading()

#Area filter
glac_shp_lyr.SetAttributeFilter("Area > %s" % min_glac_area)
feat_count = glac_shp_lyr.GetFeatureCount()
print("Min. Area filter glacier polygon count: %i" % feat_count)
glac_shp_lyr.ResetReading()

print("Processing %i features" % feat_count)

if not os.path.exists(outdir):  
    os.makedirs(outdir)

#Set higher stripe count so we're not thrashing one disk
cmd = ['lfs', 'setstripe', '-c', str(nproc), outdir]
subprocess.call(cmd)

#Create a list of glacfeat objects (contains geom) - safe for multiprocessing, while OGR layer is not
if os.path.exists(glacfeat_fn):
    print("Loading %s" % glacfeat_fn)
    glacfeat_list = pickle.load(open(glacfeat_fn,"rb"))
else:
    glacfeat_list = []
    print("Generating %s" % glacfeat_fn)
    for n, feat in enumerate(glac_shp_lyr):
        gf = GlacFeat(feat, glacname_fieldname, glacnum_fieldname)
        print("%i of %i: %s" % (n+1, feat_count, gf.feat_fn))
        #Calculate area, extent, centroid
        #NOTE: Input must be in projected coordinate system, ideally equal area
        #Should check this and reproject
        gf.geom_attributes(srs=aea_srs)
        glacfeat_list.append(gf)
    pickle.dump(glacfeat_list, open(glacfeat_fn,"wb"))

glac_shp_lyr = None
glac_shp_ds = None

def mb_calc(gf, verbose=verbose):
    #print("\n%i of %i: %s\n" % (n+1, len(glacfeat_list), gf.feat_fn))

    #This should already be handled by earlier attribute filter, but RGI area could be wrong
    #24k shp has area in m^2, RGI in km^2
    #if gf.glac_area/1E6 < min_glac_area:
    if gf.glac_area < min_glac_area:
        if verbose:
            print("Glacier area below %0.1f km2 threshold" % min_glac_area)
        return None

    #Warp everything to common res/extent/proj
    ds_list = warplib.memwarp_multi_fn([z1_fn, z2_fn], res='min', \
            extent=gf.glac_geom_extent, t_srs=aea_srs, verbose=verbose)

    if site == 'conus':
        #Add prism datasets
        prism_fn_list = [prism_ppt_annual_fn, prism_tmean_annual_fn]
        prism_fn_list.extend([prism_ppt_summer_fn, prism_ppt_winter_fn, prism_tmean_summer_fn, prism_tmean_winter_fn])
        ds_list.extend(warplib.memwarp_multi_fn(prism_fn_list, res=ds_list[0], extent=gf.glac_geom_extent, t_srs=aea_srs, verbose=verbose))

    #Check to see if z2 is empty, as z1 should be continuous
    gf.z2 = iolib.ds_getma(ds_list[1])
    if gf.z2.count() == 0:
        if verbose:
            print("No z2 pixels")
        return None

    glac_geom_mask = geolib.geom2mask(gf.glac_geom, ds_list[0])
    gf.z1 = np.ma.array(iolib.ds_getma(ds_list[0]), mask=glac_geom_mask)
    #Apply SRTM penetration correction
    if srtm_penetration_corr:
        gf.z1 = srtm_corr(gf.z1)
    gf.z2 = np.ma.array(gf.z2, mask=glac_geom_mask)
    gf.dz = gf.z2 - gf.z1
    if gf.dz.count() == 0:
        if verbose:
            print("No valid dz pixels")
        return None 

    #Should add better filtering here
    #Elevation dependent abs. threshold filter?

    filter_outliers = True 
    #Remove clearly bogus pixels
    if filter_outliers:
        bad_perc = (0.1, 99.9)
        #bad_perc = (1, 99)
        rangelim = malib.calcperc(gf.dz, bad_perc)
        gf.dz = np.ma.masked_outside(gf.dz, *rangelim)

    gf.res = geolib.get_res(ds_list[0])
    valid_area = gf.dz.count()*gf.res[0]*gf.res[1]
    valid_area_perc = valid_area/gf.glac_area
    if valid_area_perc < min_valid_area_perc:
        if verbose:
            print("Not enough valid pixels. %0.1f%% percent of glacier polygon area" % (100*valid_area_perc))
        return None

    #Rasterize NED source dates
    if site == 'conus':
        z1_date_r_ds = iolib.mem_drv.CreateCopy('', ds_list[0])
        gdal.RasterizeLayer(z1_date_r_ds, [1], z1_date_shp_lyr, options=["ATTRIBUTE=S_DATE_CLN"])
        z1_date = np.ma.array(iolib.ds_getma(z1_date_r_ds), mask=glac_geom_mask)

    #Filter dz - throw out abs differences >150 m

    #Compute dz, volume change, mass balance and stats
    gf.z1_stats = malib.get_stats(gf.z1)
    gf.z2_stats = malib.get_stats(gf.z2)
    z2_elev_med = gf.z2_stats[5]
    z2_elev_p16 = gf.z2_stats[11]
    z2_elev_p84 = gf.z2_stats[12]

    #Caluclate stats for aspect and slope using z2
    #Requires GDAL 2.1+
    gf.z2_aspect = np.ma.array(geolib.gdaldem_mem_ds(ds_list[1], processing='aspect', returnma=True), mask=glac_geom_mask)
    gf.z2_aspect_stats = malib.get_stats(gf.z2_aspect)
    z2_aspect_med = gf.z2_aspect_stats[5]
    gf.z2_slope = np.ma.array(geolib.gdaldem_mem_ds(ds_list[1], processing='slope', returnma=True), mask=glac_geom_mask)
    gf.z2_slope_stats = malib.get_stats(gf.z2_slope)
    z2_slope_med = gf.z2_slope_stats[5]

    #These can be timestamp arrays or datetime objects
    gf.t1 = z1_date
    gf.t2 = z2_date
    #This is decimal years
    gf.dt = gf.t2 - gf.t1
    if isinstance(gf.dt, timedelta):
        gf.dt = gf.dt.total_seconds()/timelib.spy
    #m/yr
    gf.dhdt = gf.dz/gf.dt
    gf.dhdt_stats = malib.get_stats(gf.dhdt)
    dhdt_mean = gf.dhdt_stats[3]
    dhdt_med = gf.dhdt_stats[5]

    #Output mean values for timestamp arrays
    if site == 'conus':
        gf.t1 = gf.t1.mean()
        gf.dt = gf.dt.mean()

    if isinstance(gf.t1, datetime):
        gf.t1 = timelib.dt2decyear(gf.t1)

    if isinstance(gf.t2, datetime):
        gf.t2 = timelib.dt2decyear(gf.t2)

    rho_i = 0.91
    rho_s = 0.50
    rho_f = 0.60
    rho_is = 0.85
    #Can estimate ELA values computed from hypsometry and typical AAR
    #For now, assume ELA is mean
    gf.z1_ela = None
    gf.z1_ela = gf.z1_stats[3]
    gf.z2_ela = gf.z2_stats[3]
    #Note: in theory, the ELA should get higher with mass loss
    #In practice, using mean and same polygon, ELA gets lower as glacier surface thins
    if verbose:
        print("ELA(t1): %0.1f" % gf.z1_ela)
        print("ELA(t2): %0.1f" % gf.z2_ela)

    if gf.z1_ela > gf.z2_ela:
        min_ela = gf.z2_ela
        max_ela = gf.z1_ela
    else:
        min_ela = gf.z1_ela
        max_ela = gf.z2_ela

    gf.mb = gf.dhdt * rho_is

    """
    # This attempted to assign different densities above and below ELA
    if gf.z1_ela is None:
        gf.mb = gf.dhdt * rho_is
    else:
        #Initiate with average density
        gf.mb = gf.dhdt*(rho_is + rho_f)/2.
        #Everything that is above ELA at t2 is elevation change over firn, use firn density
        accum_mask = (gf.z2 > gf.z2_ela).filled(0).astype(bool)
        gf.mb[accum_mask] = (gf.dhdt*rho_f)[accum_mask]
        #Everything that is below ELA at t1 is elevation change over ice, use ice density
        abl_mask = (gf.z1 <= gf.z1_ela).filled(0).astype(bool)
        gf.mb[abl_mask] = (gf.dhdt*rho_is)[abl_mask]
        #Everything in between, use average of ice and firn density
        #mb[(z1 > z1_ela) || (z2 <= z2_ela)] = dhdt*(rhois + rho_f)/2.
        #Linear ramp
        #rho_f + z2*((rho_is - rho_f)/(z2_ela - z1_ela))
        #mb = np.where(dhdt < ela, dhdt*rho_i, dhdt*rho_s)
    """

    #Use this for winter balance
    #mb = dhdt * rho_s

    gf.mb_stats = malib.get_stats(gf.mb)
    gf.mb_mean = gf.mb_stats[3]
    dmbdt_total_myr = gf.mb_mean*gf.glac_area
    mb_sum = np.sum(gf.mb)*gf.res[0]*gf.res[1]

    rho_sigma = 0.03
    dz_sigma = np.sqrt(z1_sigma**2 + z2_sigma**2)
    dhdt_sigma = dz_sigma/gf.dt

    #This is an uncertainty map
    gf.mb_sigma = np.ma.abs(gf.mb) * np.sqrt((rho_sigma/rho_is)**2 + (dhdt_sigma/gf.dhdt)**2)
    gf.mb_sigma_stats = malib.get_stats(gf.mb_sigma)
    #This is average uncertainty
    gf.mb_sigma_mean = gf.mb_sigma_stats[3]

    area_sigma_perc = 0.09 

    outlist = [gf.glacnum, gf.cx, gf.cy, z2_elev_med, z2_elev_p16, z2_elev_p84, z2_slope_med, z2_aspect_med, \
            gf.mb_mean, gf.mb_sigma_mean, (gf.glac_area/1E6), gf.t1, gf.t2, gf.dt]

    if site == 'conus':
        prism_ppt_annual = np.ma.array(iolib.ds_getma(ds_list[2]), mask=glac_geom_mask)/1000.
        prism_ppt_annual_stats = malib.get_stats(prism_ppt_annual)
        prism_ppt_annual_mean = prism_ppt_annual_stats[3]

        prism_tmean_annual = np.ma.array(iolib.ds_getma(ds_list[3]), mask=glac_geom_mask)
        prism_tmean_annual_stats = malib.get_stats(prism_tmean_annual)
        prism_tmean_annual_mean = prism_tmean_annual_stats[3]

        outlist.extend([prism_ppt_annual_mean, prism_tmean_annual_mean])

        #This is mean monthly summer precip, need to multiply by nmonths to get cumulative
        n_summer = 4
        prism_ppt_summer = n_summer * np.ma.array(iolib.ds_getma(ds_list[4]), mask=glac_geom_mask)/1000.
        prism_ppt_summer_stats = malib.get_stats(prism_ppt_summer)
        prism_ppt_summer_mean = prism_ppt_summer_stats[3]

        n_winter = 8
        prism_ppt_winter = n_winter * np.ma.array(iolib.ds_getma(ds_list[5]), mask=glac_geom_mask)/1000.
        prism_ppt_winter_stats = malib.get_stats(prism_ppt_winter)
        prism_ppt_winter_mean = prism_ppt_winter_stats[3]

        prism_tmean_summer = np.ma.array(iolib.ds_getma(ds_list[6]), mask=glac_geom_mask)
        prism_tmean_summer_stats = malib.get_stats(prism_tmean_summer)
        prism_tmean_summer_mean = prism_tmean_summer_stats[3]

        prism_tmean_winter = np.ma.array(iolib.ds_getma(ds_list[7]), mask=glac_geom_mask)
        prism_tmean_winter_stats = malib.get_stats(prism_tmean_winter)
        prism_tmean_winter_mean = prism_tmean_winter_stats[3]

        outlist.extend([prism_ppt_summer_mean, prism_ppt_winter_mean, prism_tmean_summer_mean, prism_tmean_winter_mean])

    if verbose:
        print('Mean mb: %0.2f mwe/yr' % gf.mb_mean)
        print('Sum/Area mb: %0.2f mwe/yr' % (mb_sum/gf.glac_area))
        print('Mean mb * Area: %0.2f mwe/yr' % dmbdt_total_myr)
        print('Sum mb: %0.2f mwe/yr' % mb_sum)
        #print('-------------------------------')

    #Write to master list
    #out.append(outlist)
    #Write to temporary file
    #writer.writerow(outlist)
    #f.flush()

    if writeout and (gf.glac_area/1E6 > min_glac_area_writeout):
        out_dz_fn = os.path.join(outdir, gf.feat_fn+'_dz.tif')
        iolib.writeGTiff(gf.dz, out_dz_fn, ds_list[0])

        gf.out_z1_fn = os.path.join(outdir, gf.feat_fn+'_z1.tif')
        iolib.writeGTiff(gf.z1, gf.out_z1_fn, ds_list[0])

        gf.out_z2_fn = os.path.join(outdir, gf.feat_fn+'_z2.tif')
        iolib.writeGTiff(gf.z2, gf.out_z2_fn, ds_list[0])

        temp_fn = os.path.join(outdir, gf.feat_fn+'_z2_aspect.tif')
        iolib.writeGTiff(gf.z2_aspect, temp_fn, ds_list[0])

        temp_fn = os.path.join(outdir, gf.feat_fn+'_z2_slope.tif')
        iolib.writeGTiff(gf.z2_slope, temp_fn, ds_list[0])

        if site == 'conus':
            out_z1_date_fn = os.path.join(outdir, gf.feat_fn+'_ned_date.tif')
            iolib.writeGTiff(z1_date, out_z1_date_fn, ds_list[0])

            out_prism_ppt_annual_fn = os.path.join(outdir, gf.feat_fn+'_precip_annual.tif')
            iolib.writeGTiff(prism_ppt_annual, out_prism_ppt_annual_fn, ds_list[0])
            out_prism_tmean_annual_fn = os.path.join(outdir, gf.feat_fn+'_tmean_annual.tif')
            iolib.writeGTiff(prism_tmean_annual, out_prism_tmean_annual_fn, ds_list[0])

            out_prism_ppt_summer_fn = os.path.join(outdir, gf.feat_fn+'_precip_summer.tif')
            iolib.writeGTiff(prism_ppt_summer, out_prism_ppt_summer_fn, ds_list[0])
            out_prism_ppt_winter_fn = os.path.join(outdir, gf.feat_fn+'_precip_winter.tif')
            iolib.writeGTiff(prism_ppt_winter, out_prism_ppt_winter_fn, ds_list[0])

            out_prism_tmean_summer_fn = os.path.join(outdir, gf.feat_fn+'_tmean_summer.tif')
            iolib.writeGTiff(prism_tmean_summer, out_prism_tmean_summer_fn, ds_list[0])
            out_prism_tmean_winter_fn = os.path.join(outdir, gf.feat_fn+'_tmean_winter.tif')
            iolib.writeGTiff(prism_tmean_winter, out_prism_tmean_winter_fn, ds_list[0])

    #Do AED for all
    #Compute mb using scaled AED vs. polygon
    #Extract slope and aspect numbers for polygon
    #Check for valid pixel count vs. feature area, fill if appropriate

    #Error analysis assuming date is wrong by +/- 1-2 years

    if mb_plot and (gf.glac_area/1E6 > min_glac_area_writeout):
        #Now just pass gf to these functions
        z_bin_edges = hist_plot(gf, outdir)
        map_plot(gf, z_bin_edges, outdir)

    return outlist, gf

# For testing
#glacfeat_list_in = glacfeat_list[0:20]
glacfeat_list_in = glacfeat_list
glacfeat_list_out = []

if parallel:
    print("Running in parallel")
    from multiprocessing import Pool
    # By default, use all cores - 1
    p = Pool(nproc)

    """
    # Simple mapping
    # No progress bar
    #out = p.map(mb_calc, glacfeat_list_in)
    """

    """
    # Using imap_unordered - apparently slower than map_async
    results = p.imap_unordered(mb_calc, glacfeat_list_in, 1)
    p.close()
    import time
    while True:
        ndone = results._index
        if ndone == len(glacfeat_list_in): break
        print('%i of %i done' % (ndone, len(glacfeat_list_in)))
        time.sleep(1)
        #sys.stderr.write('%i of %i done' % (i, len(glacfeat_list))) 
    out = [j for j in results]
    """

    # Using map_async
    results = p.map_async(mb_calc, glacfeat_list_in, 1)
    p.close()
    import time
    while True:
        if results.ready(): break
        ndone = len(glacfeat_list_in) - results._number_left
        print('%i of %i done' % (ndone, len(glacfeat_list_in)))
        time.sleep(2)
        #sys.stderr.write('%i of %i done' % (i, len(glacfeat_list))) 
    out = results.get()
else:
    print("Running serially")
    for n, gf in enumerate(glacfeat_list_in):
        print('%i of %i: %s' % (n, len(glacfeat_list_in), gf.feat_fn))
        out.append(mb_calc(gf))

mb_list = []
for i in out:
    if i is not None:
        mb_list.append(i[0])
        glacfeat_list_out.append(i[1])
out = np.array(mb_list, dtype=float)

#Sort by area
out = out[out[:,3].argsort()[::-1]]

out_header = '%s,x,y,z_med,z_p16,z_p84,z_slope,z_aspect,mb_mwea,mb_sigma_mwea,area_km2,t1,t2,dt' % glacnum_fieldname
if site == 'conus':
    out_header += ',ppt_a,tmean_a'
    out_header += ',ppt_s,ppt_w,tmean_s,tmean_w'

out_fmt = [glacnum_fmt,] + ['%0.2f'] * (out.shape[1] - 1)

np.savetxt(out_fn, out, fmt=out_fmt, delimiter=',', header=out_header, comments='')

#Now join with geopandas

"""
#out_rgiid = ['RGI60-%08.5f' % i for i in out[:,0]]

#Write out the populated glacfeat objects (contain raster grids, stats, etc)
#Should compress with gzip module
#glacfeat_fn_out = os.path.splitext(glacfeat_fn)[0]+'_out.p'
#pickle.dump(glacfeat_list_out, open(glacfeat_fn_out,"wb"))

#Transfer maps, figs, etc for largest glaciers
#scpput $(ls $(ls -Sr *dz.tif | tail -n 20 | awk -F'_dz' '{print $1}' | sed 's/$/*png/')) /tmp/hma_png
"""

import os
import sys
from itertools import zip_longest
import argparse as ap
import numpy as np
import scipy.linalg as la
from scipy.spatial.transform import Rotation as R
import matplotlib.pyplot as plt
import yaml
import sotodlib.io.g3tsmurf_utils as g3u
from sotodlib.core import AxisManager, metadata, Context
from sotodlib.io.metadata import read_dataset, write_dataset
from sotodlib.site_pipeline import util
from sotodlib.coords import optics as op

logger = util.init_logger(__name__, "finalize_focal_plane: ")


def _avg_focalplane(fp_dict):
    focal_plane = []
    det_ids = np.array(list(fp_dict.keys()))
    for did in det_ids:
        avg_pointing = np.nanmedian(np.vstack(fp_dict[did]), axis=0)
        focal_plane.append(avg_pointing)
    focal_plane = np.column_stack(focal_plane)

    if np.isnan(focal_plane[:2]).all():
        raise ValueError("All detectors are outliers. Check your inputs")

    return det_ids, focal_plane


def _mk_fpout(det_id, focal_plane):
    outdt = [
        ("dets:det_id", det_id.dtype),
        ("xi", np.float32),
        ("eta", np.float32),
        ("gamma", np.float32),
    ]
    fpout = np.fromiter(zip(det_id, *focal_plane[:3]), dtype=outdt, count=len(det_id))

    return metadata.ResultSet.from_friend(fpout)


def _mk_tpout(shift, scale, shear, rot):
    outdt = [
        ("shift", np.float32),
        ("scale", np.float32),
        ("shear", np.float32),
        ("rot", np.float32),
    ]
    # rot will always have 3 values
    # so we can use to pad the others when we have no pol
    tpout = np.fromiter(
        zip_longest(shift, scale, shear, rot, fillvalue=np.nan), count=3, dtype=outdt
    )

    return metadata.ResultSet.from_friend(tpout)


def _mk_plot(nominal, measured, affine, shift, show_or_save):
    plt.style.use("tableau-colorblind10")
    _, ax = plt.subplots()
    ax.set_xlabel("Xi Nominal (rad)")
    ax.set_ylabel("Eta Nominal (rad)")
    p1 = ax.scatter(nominal[0], nominal[1], label="nominal", color="grey")
    ax1 = ax.twinx()
    ax1.set_ylabel("Eta Measured (rad)")
    ax2 = ax1.twiny()
    ax2.set_xlabel("Xi Measured (rad)")
    p2 = ax2.scatter(measured[0], measured[1], label="measured")
    transformed = affine @ nominal + shift[:, None]
    p3 = ax2.scatter(transformed[0], transformed[1], label="transformed")
    ax2.legend(handles=[p1, p2, p3])
    if isinstance(show_or_save, str):
        plt.savefig(show_or_save)
        plt.cla()
    else:
        plt.show()


def get_nominal(focal_plane, config):
    """
    Get nominal pointing from detector xy positions.

    Arguments:

        focal_plane: Focal plane array as generated by _avg_focalplane.

        config: Transformation configuration.
                Nominally config["coord_transform"].

    Returns:

        xi_nominal: The nominal xi values.

        eta_nominal: The nominal eta values.

        gamma_nominal: The nominal gamma values.
    """
    transform_pars = op.get_ufm_to_fp_pars(
        config["telescope"], config["slot"], config["config_path"]
    )
    x, y, pol = op.ufm_to_fp(
        None, x=focal_plane[3], y=focal_plane[4], pol=focal_plane[5], **transform_pars
    )
    if config["telescope"] == "LAT":
        xi_nominal, eta_nominal, gamma_nominal = op.LAT_focal_plane(
            None, config["zemax_path"], x, y, pol, config["rot"], config["tube"]
        )
    elif config["coord_transform"]["telescope"] == "SAT":
        xi_nominal, eta_nominal, gamma_nominal = op.SAT_focal_plane(None, x, y, pol)
    else:
        raise ValueError("Invalid telescope provided")

    return xi_nominal, eta_nominal, gamma_nominal


def main():
    # Read in input pars
    parser = ap.ArgumentParser()

    parser.add_argument("config_path", help="Location of the config file")
    args = parser.parse_args()

    # Open config file
    with open(args.config_path, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    # Load context
    ctx = Context(config["context"]["path"])
    name = config["context"]["position_match"]
    query = []
    if "query" in config["context"]:
        query = (ctx.obsdb.query(config["context"]["query"])["obs_id"],)
    obs_ids = np.append(config["context"].get("obs_ids", []), query)
    # Add in manually loaded paths
    obs_ids = np.append(obs_ids, config.get("multi_obs", []))
    if len(obs_ids) == 0:
        raise ValueError("No position match results provided in configuration")
    detmaps = config["detmaps"]
    if len(obs_ids) != len(detmaps):
        raise ValueError(
            "Number of DetMaps doesn't match number of position match results"
        )

    # Build output path
    ufm = config["ufm"]
    append = ""
    if "append" in config:
        append = "_" + config["append"]
    os.makedirs(config["outdir"], exist_ok=True)
    outpath = os.path.join(config["outdir"], f"{ufm}{append}.h5")
    outpath = os.path.abspath(outpath)

    fp_dict = {}
    use_matched = config.get("use_matched", False)
    for obs_id, detmap in zip(obs_ids, detmaps):
        # Load data
        if os.path.isfile(obs_id):
            logger.info("Loading information from file at %s", obs_id)
            rset = read_dataset(obs_id, "focal_plane")
            _aman = rset.to_axismanager(axis_key="dets:readout_id")
            aman = AxisManager(_aman.dets)
            aman.wrap(name, _aman)
        else:
            logger.info("Loading information from observation %s", obs_id)
            aman = ctx.get_meta(obs_id, dets=config["context"].get("dets", {}))
        if name not in aman:
            logger.warning(
                "\tNo position_match associated with this observation. Skipping."
            )
            continue

        # Put SMuRF band channel in the correct place
        smurf = AxisManager(aman.dets)
        smurf.wrap("band", aman[name].band, [(0, smurf.dets)])
        smurf.wrap("channel", aman[name].channel, [(0, smurf.dets)])
        aman.det_info.wrap("smurf", smurf)

        if detmap is not None:
            g3u.add_detmap_info(aman, detmap)
        have_wafer = "wafer" in aman.det_info
        if not have_wafer:
            logger.error("\tThis observation has no detmap results, skipping")
            continue

        det_ids = aman.det_info.det_id
        x = aman.det_info.wafer.det_x
        y = aman.det_info.wafer.det_y
        pol = aman.det_info.wafer.angle
        if use_matched:
            det_ids = aman[name].matched_det_id
            dm_sort = np.argsort(aman.det_info.det_id)
            mapping = np.argsort(np.argsort(det_ids))
            x = x[dm_sort][mapping]
            y = y[dm_sort][mapping]
            pol = pol[dm_sort][mapping]

        focal_plane = np.column_stack(
            (aman[name].xi, aman[name].eta, aman[name].polang, x, y, pol)
        ).astype(float)
        out_msk = aman[name].outliers
        focal_plane[out_msk, :3] = np.nan

        for di, fp in zip(det_ids, focal_plane):
            try:
                fp_dict[di].append(fp)
            except KeyError:
                fp_dict[di] = [fp]

    if not fp_dict:
        logger.error("No valid observations provided")
        sys.exit()

    # Compute the average focal plane while ignoring outliers
    det_id, focal_plane = _avg_focalplane(fp_dict)
    measured = focal_plane[:3]

    # Get nominal xi, eta, gamma
    nominal = get_nominal(focal_plane, config["coord_transform"])

    # Compute transformation between the two nominal and measured pointing
    if np.isnan(measured[2]).all():
        logger.warning("No polarization data availible, gammas will be nan")
        nominal = nominal[:2]
        measured = measured[:2]
    affine, shift = op.get_affine(np.vstack(nominal), np.vstack(measured))
    scale, shear, rot = op.decompose_affine(affine)
    rot = op.decompose_rotation(rot)

    if np.isclose(scale, np.pi / 180.0).any() or np.isclose(scale, 180.0 / np.pi).any():
        logger.warning(
            (
                "Scale factor looks like a deg/rad conversion."
                " Someone may have used the wrong units somewhere."
            )
        )

    plot = config.get("plot", False)
    if plot:
        _mk_plot(nominal, measured, affine, shift, plot)

    # Make final outputs and save
    logger.info("Saving data to %s", outpath)
    fpout = _mk_fpout(det_id, focal_plane)
    tpout = _mk_tpout(shift, scale, shear, rot)
    write_dataset(fpout, outpath, "focal_plane", overwrite=True)
    write_dataset(tpout, outpath, "pointing_transform", overwrite=True)


if __name__ == "__main__":
    main()

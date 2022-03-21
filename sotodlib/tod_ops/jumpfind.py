import numpy as np
import scipy.signal as sig
import scipy.ndimage as simg


def _jumpfind(x, min_chunk, min_size, win_size, max_depth=-1, depth=0, **kwargs):
    """
    Recursive edge detection based jumpfinder.

    Note that the jumpfinder is very sensitive to changes in parameters
    and the parameters are not independant of each other,
    so it may take some playing around to get it to work properly.

    Arguments:

        x: Data to jumpfind on, expects 1D
           Note that x will by modified in place with jumps removed
           pass x.copy() to preserve x (say to pass to a better jump fixer)

        min_chunk: The smallest chunk of data to look for jumps in

        min_size: The smalled jump size counted as a jump

        win_size: Number of samples to average over when checking jump size

        max_depth: The maximum recursion depth, set negetive to not use

        depth: The current recursion depth

        **kwargs: Arguments to pass to scipy.signal.find_peaks

    Returns:

        jumps: The indices of jumps in x
               There is some uncertainty on order of 1 sample
               Jumps within min_chunk of each other may not be distinguished
    """
    if len(x) < min_chunk:
        return np.array([])
    # Make step to convolve data with
    step = np.ones(2 * len(x))
    step[len(x) :] = -1
    # Mean subtract the data
    x -= x.mean()

    # Convolve and find jumps
    x_step = sig.convolve(x, step, "valid")
    u_jumps, _ = sig.find_peaks(x_step, **kwargs)
    d_jumps, _ = sig.find_peaks(-1 * x_step, **kwargs)
    jumps = np.concatenate([u_jumps, d_jumps])
    jumps.sort()

    # Filter out jumps that are too small
    j_i = []
    for i, j in enumerate(jumps):
        if i + 1 < len(jumps):
            right = min(j + win_size, len(x), jumps[i + 1] - min_chunk)
        else:
            right = min(j + win_size, len(x))
        if i > 0:
            left = max(j - win_size, 0, jumps[i - 1] + min_chunk)
        else:
            left = max(j - win_size, 0)

        if (
            abs(
                np.median(x[j + min_chunk : right]) - np.median(x[left : j - min_chunk])
            )
            > min_size
        ):
            j_i.append(i)
    jumps = jumps[j_i]

    # If no jumps found return
    if len(jumps) == 0:
        return jumps

    # If at max_depth then return
    if depth == max_depth:
        return jumps

    # Recursively check for jumps between jumps
    _jumps = np.insert(jumps, 0, 0)
    _jumps = np.append(_jumps, len(x))
    added = 0
    for i in range(len(_jumps) - 1):
        sub_jumps = jumpfind(
            x[(_jumps[i]) : (_jumps[i + 1])],
            min_chunk,
            min_size,
            win_size,
            max_depth,
            depth + 1,
            **kwargs
        )
        jumps = np.insert(jumps, i + added, sub_jumps + _jumps[i])
        added += len(sub_jumps)
    return jumps


def filter_and_jumpfind(
    x, sigma, order, min_chunk, min_size, abs_min_size, win_size, max_depth, **kwargs
):
    """
    Apply filter to data and then search for jumps

    Note that the jumpfinder is very sensitive to changes in parameters
    and the parameters are not independant of each other,
    so it may take some playing around to get it to work properly

    Arguments:

        x: Data to jumpfind on, expects 1D

        sigma: Sigma of gaussain kernal

        order: Order of gaussain filter
               Note the following:
               Order 0 works a bit better than just calling jumpfind
               Order 1 is not reccomended, it can catch both jumps and spikes
               but it cant't distinguish them and misses jumps
               Order 2 works well to catch jumps and has a low false negetive rate
               but it can get confused near large spikes

        param min_chunk: The smallest chunk of data to look for jumps in

        param min_size: The smalled jump size counted as a jump
                        Note that this is in terms of the filtered data

        param abs_min_size: The minimum size of jumps in the unfiltered data
                            Note that for order 0 this is not used

        param win_size: Number of samples to average over when checking jump size
                        For order 2, 2*sigma works well

        max_depth: The max recursion depth, set negetive to not use

        **kwargs: Arguments to pass to scipy.signal.find_peaks

    Returns:

        jumps: The indices of jumps in x
               There is some uncertainty on order of 1 sample
               Jumps within min_chunk of each other may not be distinguished
    """
    # Apply filter
    x_filt = simg.gaussian_filter(x, sigma, order)

    # Search for jumps in filtered data
    jumps = _jumpfind(x_filt, min_chunk, min_size, win_size, max_depth, 0, **kwargs)

    if order == 0:
        return jumps

    # Filter out jumps that are too small
    j_i = []
    for i, j in enumerate(jumps):
        if i + 1 < len(jumps):
            right = min(j + win_size, len(x), jumps[i + 1] - min_chunk)
        else:
            right = min(j + win_size, len(x))
        if i > 0:
            left = max(j - win_size, 0, jumps[i - 1] + min_chunk)
        else:
            left = max(j - win_size, 0)

        if (
            abs(
                np.median(x[j + min_chunk : right]) - np.median(x[left : j - min_chunk])
            )
            > abs_min_size
        ):
            j_i.append(i)
    return jumps[j_i]


def jumpfind_default_pars(x, sensitivity=2.0):
    """
    Calculate (semi) intelligent default parameters and jumpfind with them
    The presence of large spikes can have a negetive effect on this function,
    please mask/slice/interpolate them out before using this functon.

    Tested mostly on LATR data and seems to be working well.
    Limited tests with LATRT data have also gone well.

    Arguments:

        x: Data to jumpfind on, expects 1D

        sensitivity: Sensitivity of the jumpfinder, roughly correlates with
                     1/(jump size) but since data is filtered, detrended, and
                     rescaled during jumpfinding it is better to think of it as
                     something non-physical.

    Returns:

        jumps: The indices of jumps in x
               There is some uncertainty on order of a few samples
               Jumps within 20 samples of each other may not be distinguished
    """
    _x = sig.detrend(x)
    if _x.std() > 10:
        _x = _x/(10**(int(np.log10(_x.std()))))
    if np.isclose(_x.std(), 0.0):
        return np.array([])

    return filter_and_jumpfind(
        _x,
        2*_x.std(),
        0,
        10,
        1.0/sensitivity,
        0,
        20,
        0,
        height=1,
        prominence=1,
    )


def jumpfind_default_pars_recursive(x, sensitivity=2.0, max_depth=-1, depth=0):
    """
    Recursively run jumpfind_default_pars
    This helps find jumps that are missed due to jumpfind_default_pars using std to determine some
    params since std is sensitive to the presence of jumps.
    Using a jump agnostic metric instead should alleviate the need for this function.

    Tested mostly on LATR data and seems to be working well.
    Limited tests with LATRT data have also gone well.

    Arguments:

        x: Data to jumpfind on, expects 1D

        sensitivity: Sensitivity of the jumpfinder, roughly correlates with
                     1/(jump size) but since data is filtered, detrended, and
                     rescaled during jumpfinding it is better to think of it as
                     something non-physical.

        max_depth: The maximum recursion depth, set negetive to not use

        depth: The current recursion depth

    Returns:

        jumps: The indices of jumps in x
               There is some uncertainty on order of a few samples
               Jumps within 20 samples of each other may not be distinguished
    """
    # Find jumps
    jumps = jumpfind_default_pars(x, sensitivity)

    # If no jumps found return
    if len(jumps) == 0:
        return jumps

    # If at max_depth then return
    if depth == max_depth:
        return jumps

    # Recursively check for jumps between jumps
    _jumps = np.insert(jumps, 0, 0)
    _jumps = np.append(_jumps, len(x))
    added = 0
    for i in range(len(_jumps) - 1):
        sub_jumps = jumpfind_default_pars_recursive(
            x[(_jumps[i]) : (_jumps[i + 1])], sensitivity, max_depth, depth + 1
        )
        jumps = np.insert(jumps, i + added, sub_jumps + _jumps[i])
        added += len(sub_jumps)
    return jumps


def jumpfind(tod, signal=None, sensitivity=2.0):
    """
    Find jumps in tod.signal_name.
    Expects tod.signal_name to be 1D of 2D

    Arguments:

        tod: axis manager

        signal: Signal to jumpfind on. If None than tod.signal is used.

        sensitivity: Sensitivity of the jumpfinder, roughly correlates with
                     1/(jump size) but since data is filtered, detrended, and
                     rescaled during jumpfinding it is better to think of it as
                     something non-physical.

    Returns:

        jumps: The indices of jumps in x
               There is some uncertainty on order of a few samples
               Jumps within 20 samples of each other may not be distinguished
    """
    if signal is None:
        signal = tod.signal

    if len(signal.shape) == 1:
        return jumpfind_default_pars_recursive(signal, sensitivity)
    elif len(signal.shape) == 2:
        return [jumpfind_default_pars_recursive(sig, sensitivity) for sig in signal]
    else:
        raise ValueError("Jumpfinder only works on 1D or 2D data")

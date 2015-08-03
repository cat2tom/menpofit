from __future__ import division
from functools import partial
import itertools
import numpy as np
from menpo.visualize import progress_bar_str, print_dynamic, print_progress


def name_of_callable(c):
    try:
        if isinstance(c, partial):  # partial
            # Recursively call as partial may be wrapping either a callable
            # or a function (or another partial for some reason!)
            return name_of_callable(c.func)
        else:
            return c.__name__  # function
    except AttributeError:
        return c.__class__.__name__  # callable class


def batch(iterable, n):
    it = iter(iterable)
    while True:
        chunk = tuple(itertools.islice(it, n))
        if not chunk:
            return
        yield chunk


def is_pyramid_on_features(features):
    r"""
    True if feature extraction happens once and then a gaussian pyramid
    is taken. False if a gaussian pyramid is taken and then features are
    extracted at each level.
    """
    return callable(features)


def create_pyramid(images, n_levels, downscale, features, verbose=False):
    r"""
    Function that creates a generator function for Gaussian pyramid. The
    pyramid can be created either on the feature space or the original
    (intensities) space.

    Parameters
    ----------
    images: list of :map:`Image`
        The set of landmarked images from which to build the AAM.

    n_scales: int
        The number of multi-resolution pyramidal levels to be used.

    downscale: float
        The downscale factor that will be used to create the different
        pyramidal levels.

    features: ``callable`` ``[callable]``
        If a single callable, then the feature calculation will happen once
        followed by a gaussian pyramid. If a list of callables then a
        gaussian pyramid is generated with features extracted at each level
        (after downsizing and blurring).

    Returns
    -------
    list of generators :
        The generator function of the Gaussian pyramid.

    """
    will_take_a_while = is_pyramid_on_features(features)
    if will_take_a_while and verbose:
        images = print_progress(images, show_bar=False, show_count=False,
                                prefix='- Computing top-level feature space')
    pyramids = []
    for img in images:
        pyramids.append(pyramid_of_feature_images(n_levels, downscale,
                                                  features, img))
    return pyramids


def pyramid_of_feature_images(n_levels, downscale, features, image):
    r"""
    Generates a gaussian pyramid of feature images for a single image.
    """
    if is_pyramid_on_features(features):
        # compute feature image at the top
        feature_image = features(image)
        # create pyramid on the feature image
        return feature_image.gaussian_pyramid(n_levels=n_levels,
                                              downscale=downscale)
    else:
        # create pyramid on intensities image
        # feature will be computed per level
        pyramid = image.gaussian_pyramid(n_levels=n_levels,
                                         downscale=downscale)
        # add the feature generation here
        return feature_images(pyramid, features)


# adds feature extraction to a generator of images
def feature_images(images, features):
    for feature, level in zip(reversed(features), images):
        yield feature(level)


class DeformableModel(object):

    def __init__(self, features):
        self.features = features

    @property
    def pyramid_on_features(self):
        return is_pyramid_on_features(self.features)


def build_grid(shape):
    r"""
    """
    shape = np.asarray(shape)
    half_shape = np.floor(shape / 2)
    half_shape = np.require(half_shape, dtype=int)
    start = -half_shape
    end = half_shape + shape % 2
    sampling_grid = np.mgrid[start[0]:end[0], start[1]:end[1]]
    return np.rollaxis(sampling_grid, 0, 3)

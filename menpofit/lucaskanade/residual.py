"""
This module contains a set of similarity measures that was designed for use
within the Lucas-Kanade framework. They therefore expose a number of methods
that make them useful for inverse compositional and forward additive
Lucas-Kanade.

These similarity measures are designed to be dimension independent where
possible. For this reason, some methods look more complicated than would be
normally the case. For example, calculating the Hessian involves summing
a multi-dimensional array, so we dynamically calculate the list of axes
to sum over. However, the basics of the logic, other than dimension
reduction, should be similar to the original algorithms.

References
----------

.. [1] Lucas, Bruce D., and Takeo Kanade.
       "An iterative image registration technique with an application to stereo
       vision."
       IJCAI. Vol. 81. 1981.
"""
import abc
import copy
import numpy as np
from numpy.fft import fftshift, fftn
import scipy.linalg

from menpo.math import log_gabor
from menpo.image import MaskedImage
from menpo.feature import gradient


class Residual(object):
    """
    An abstract base class for calculating the residual between two images
    within the Lucas-Kanade algorithm. The classes were designed
    specifically to work within the Lucas-Kanade framework and so no
    guarantee is made that calling methods on these subclasses will generate
    correct results.
    """
    __metaclass__ = abc.ABCMeta

    @property
    def error(self):
        r"""
        The RMS of the error image.

        :type: float

        Notes
        -----
        Will only generate a result if the
        :func:`steepest_descent_update` function has been calculated prior.

        .. math::
            error = \sqrt{\sum_x E(x)^2}

        where :math:`E(x) = T(x) - I(W(x;p))` within the forward additive
        framework.
        """
        return np.sqrt(np.mean(self._error_img ** 2))

    @abc.abstractmethod
    def steepest_descent_images(self, image, dW_dp, **kwargs):
        r"""
        Calculates the standard steepest descent images.

        Within the forward additive framework this is defined as

        .. math::
             \nabla I \frac{\partial W}{\partial p}

        The input image is vectorised (`N`-pixels) so that masked images can
        be handled.

        Parameters
        ----------
        image : :class:`menpo.image.base.Image`
            The image to calculate the steepest descent images from, could be
            either the template or input image depending on which framework is
            used.
        dW_dp : ndarray
            The Jacobian of the warp.

        Returns
        -------
        VT_dW_dp : (N, n_params) ndarray
            The steepest descent images
        """
        pass

    @abc.abstractmethod
    def calculate_hessian(self, VT_dW_dp):
        r"""
        Calculates the Gauss-Newton approximation to the Hessian.

        This is abstracted because some residuals expect the Hessian to be
        pre-processed. The Gauss-Newton approximation to the Hessian is
        defined as:

        .. math::
            \mathbf{J J^T}

        Parameters
        ----------
        VT_dW_dp : (N, n_params) ndarray
            The steepest descent images.

        Returns
        -------
        H : (n_params, n_params) ndarray
            The approximation to the Hessian
        """
        pass

    @abc.abstractmethod
    def steepest_descent_update(self, VT_dW_dp, IWxp, template):
        r"""
        Calculates the steepest descent parameter updates.

        These are defined, for the forward additive algorithm, as:

        .. math::
            \sum_x [ \nabla I \frac{\partial W}{\partial p} ]^T [ T(x) - I(W(x;p)) ]

        Parameters
        ----------
        VT_dW_dp : (N, n_params) ndarray
            The steepest descent images.
        IWxp : :class:`menpo.image.base.Image`
            Either the warped image or the template
            (depending on the framework)
        template : :class:`menpo.image.base.Image`
            Either the warped image or the template
            (depending on the framework)

        Returns
        -------
        sd_delta_p : (n_params,) ndarray
            The steepest descent parameter updates.
        """
        pass

    def _calculate_gradients(self, image, forward=None):
        r"""
        Calculates the gradients of the given method.

        If `forward` is provided, then the gradients are warped
        (as required in the forward additive algorithm)

        Parameters
        ----------
        image : :class:`menpo.image.base.Image`
            The image to calculate the gradients for
        forward : (:map:`Image`, :map:`AlignableTransform>`), optional
            A tuple containing the extra weights required for the function
            `warp` (which should be passed as a function handle).

            Default: `None`
        """
        if forward:
            # Calculate the gradient over the image
            # grad:  (dims x ch) x H x W
            grad = gradient(image, fast2d=False)
            # Warp gradient for forward additive using the given transform
            # grad:  (dims x ch) x h x w
            template, transform = forward
            grad = grad.warp_to_mask(template.mask, transform,
                                     warp_landmarks=False)
        else:
            # Calculate the gradient over the image and set one pixels along
            # the boundary of the image mask to zero (no reliable gradient
            # can be computed there!)
            # grad:  (dims x ch) x h x w
            grad = gradient(image, fast2d=False)
            grad.set_boundary_pixels()
        return grad


class SSD(Residual):

    type = 'SSD'

    def steepest_descent_images(self, image, dW_dp, forward=None):
        # compute gradient
        # grad:  dims x ch x (h x w)
        grad = self._calculate_gradients(image, forward=forward)
        grad = grad.as_vector().reshape((image.n_dims, image.n_channels, -1))

        # compute steepest descent images
        # gradient: n_dims x n_channels x n_pixels
        # dw_dp:    n_dims x            x n_pixels x n_params
        # sdi:               n_channels x n_pixels x n_params
        sdi = 0
        a = grad[..., None] * dW_dp[:, None, ...]
        for d in a:
            sdi += d

        # reshape steepest descent images
        # sdi: (n_channels x n_pixels) x n_params
        return sdi.reshape((-1, sdi.shape[2]))

    def calculate_hessian(self, J, J2=None):
        if J2 is None:
            H = J.T.dot(J)
        else:
            H = J.T.dot(J2)
        return H

    def steepest_descent_update(self, sdi, IWxp, template):
        self._error_img = IWxp.as_vector() - template.as_vector()
        return sdi.T.dot(self._error_img)


class GaborFourier(Residual):

    type = 'GaborFourier'

    def __init__(self, image_shape, **kwargs):
        if 'filter_bank' in kwargs:
            self._filter_bank = kwargs.get('filter_bank')
            if self._filter_bank.shape != image_shape:
                raise ValueError('Filter bank shape must match the shape '
                                 'of the image')
        else:
            gabor = log_gabor(np.ones(image_shape), **kwargs)
            # Get filter bank matrix
            self._filter_bank = gabor[2]

        # Flatten the filter bank for vectorized calculations
        self._filter_bank = self._filter_bank.flatten()

    def steepest_descent_images(self, image, dW_dp, forward=None):
        # compute gradient
        # gradient:  height  x  width  x  n_channels
        gradient_img = self._calculate_gradients(image, forward=forward)

        # reshape gradient
        # gradient:  n_pixels  x  (n_channels x n_dims)
        gradient = gradient_img.as_vector(keep_channels=True)

        # reshape gradient
        # gradient:  n_pixels  x  n_channels  x  n_dims
        gradient = np.reshape(gradient, (-1, image.n_channels, image.n_dims))

        # compute steepest descent images
        # gradient:  n_pixels  x  n_channels  x            x  n_dims
        # dW_dp:     n_pixels  x              x  n_params  x  n_dims
        # sdi:       n_pixels  x  n_channels  x  n_params
        sdi = np.sum(dW_dp[:, None, :, :] * gradient[:, :, None, :], axis=3)

        # make sdi images
        # sdi_img:  shape  x  n_channels  x  n_params
        sdi_img_channels = image.n_channels * dW_dp.shape[1]
        sdi_img = MaskedImage.blank(gradient_img.shape,
                                      n_channels=sdi_img_channels,
                                      mask=gradient_img.mask)
        sdi_img.from_vector_inplace(sdi.flatten())

        # compute FFT over each channel, parameter and dimension
        # fft_sdi:  height  x  width  x  n_channels  x  n_params
        fft_axes = range(image.n_dims)
        fft_sdi = fftshift(fftn(sdi_img.pixels, axes=fft_axes), axes=fft_axes)

        # ToDo: Note that, fft_sdi is rectangular, i.e. is not define in
        # terms of the mask pixels, but in terms of the whole image.
        # Selecting mask pixels once the fft has been computed makes no
        # sense because they have lost their original spatial meaning.

        # reshape steepest descent images
        # sdi:  (height x width x n_channels)  x  n_params
        return np.reshape(fft_sdi, (-1, dW_dp.shape[1]))

    def calculate_hessian(self, sdi):
        # reshape steepest descent images
        # sdi:  n_channels  x  n_pixels  x  n_params
        sdi = np.reshape(sdi, (-1, self._filter_bank.shape[0], sdi.shape[1]))

        # compute filtered steepest descent images
        # _filter_bank:              x  n_pixels  x
        # sdi:           n_channels  x  n_pixels  x  n_params
        # filtered_sdi:  n_channels  x  n_pixels  x  n_params
        filtered_sdi = (self._filter_bank[None, ..., None] ** 0.5) * sdi

        # reshape filtered steepest descent images
        # filtered_sdi:  (n_pixels x n_channels)  x  n_params
        filtered_sdi = np.reshape(filtered_sdi, (-1, sdi.shape[-1]))

        # compute filtered hessian
        # filtered_sdi:  (n_pixels x n_channels)  x  n_params
        # hessian:              n_param           x  n_param
        return np.conjugate(filtered_sdi).T.dot(filtered_sdi)

    def steepest_descent_update(self, sdi, IWxp, template):
        # compute error image
        # error_img:  height  x  width  x  n_channels
        error_img = IWxp.pixels - template.pixels

        # compute FFT error image
        # fft_error_img:  height  x  width  x  n_channels
        fft_axes = range(IWxp.n_dims)
        fft_error_img = fftshift(fftn(error_img, axes=fft_axes),
                                 axes=fft_axes)

        # reshape FFT error image
        # fft_error_img:  (height x width)  x  n_channels
        fft_error_img = np.reshape(fft_error_img, (-1, IWxp.n_channels))

        # compute filtered steepest descent images
        # _filter_bank:        (height x width)  x
        # fft_error_img:       (height x width)  x  n_channels
        # filtered_error_img:  (height x width)  x  n_channels
        filtered_error_img = (self._filter_bank[..., None] * fft_error_img)

        # reshape _error_img
        # _error_img:  (height x width x n_channels)
        self._error_img = filtered_error_img.flatten()

        # compute steepest descent update
        # sdi:         (height x width x n_channels)  x  n_parameters
        # _error_img:  (height x width x n_channels)
        # sdu:             n_parameters
        return sdi.T.dot(np.conjugate(self._error_img))


class ECC(Residual):

    type = 'ECC'

    def _normalise_images(self, image):
        # TODO: do we need to copy the image?
        # TODO: is this supposed to be per channel normalization?
        norm_image = image.copy()
        norm_image.normalize_norm_inplace()
        return norm_image

    def steepest_descent_images(self, image, dW_dp, forward=None):
        # normalize image
        norm_image = self._normalise_images(image)

        # compute gradient
        # gradient:  dims x ch x (h x w)
        grad = self._calculate_gradients(norm_image, forward=forward)
        grad = grad.as_vector().reshape((image.n_dims, image.n_channels, -1))

        # compute steepest descent images
        # gradient: n_dims x n_channels x n_pixels
        # dw_dp:    n_dims x            x n_pixels x n_params
        # sdi:               n_channels x n_pixels x n_params
        sdi = 0
        a = grad[..., None] * dW_dp[:, None, ...]
        for d in a:
            sdi += d

        # reshape steepest descent images
        # sdi: (n_channels x n_pixels) x n_params
        return sdi.reshape((-1, sdi.shape[2]))

    def calculate_hessian(self, sdi):
        H = sdi.T.dot(sdi)
        self._H_inv = scipy.linalg.inv(H)
        return H

    def steepest_descent_update(self, sdi, IWxp, template):
        normalised_IWxp = self._normalise_images(IWxp).as_vector()
        normalised_template = self._normalise_images(template).as_vector()

        Gt = sdi.T.dot(normalised_template)
        Gw = sdi.T.dot(normalised_IWxp)

        # Calculate the numerator
        IWxp_norm = scipy.linalg.norm(normalised_IWxp)
        num1 = IWxp_norm ** 2
        num2 = np.dot(Gw.T, np.dot(self._H_inv, Gw))
        num = num1 - num2

        # Calculate the denominator
        den1 = np.dot(normalised_template, normalised_IWxp)
        den2 = np.dot(Gt.T, np.dot(self._H_inv, Gw))
        den = den1 - den2

        # Calculate lambda to choose the step size
        # Avoid division by zero
        if den > 0:
            l = num / den
        else:
            den3 = np.dot(Gt.T, np.dot(self._H_inv, Gt))
            l1 = np.sqrt(num2 / den3)
            l2 = - den / den3
            l = np.maximum(l1, l2)

        self._error_img = l * normalised_IWxp - normalised_template

        return sdi.T.dot(self._error_img)


class GradientImages(Residual):

    type = 'GradientImages'

    def _regularise_gradients(self, grad):
        pixels = grad.pixels
        ab = np.sqrt(np.sum(pixels**2, axis=0))
        m_ab = np.median(ab)
        ab = ab + m_ab
        grad.pixels = pixels / ab
        return grad

    def steepest_descent_images(self, image, dW_dp, forward=None):
        n_dims = image.n_dims
        n_channels = image.n_channels

        # compute gradient
        first_grad = self._calculate_gradients(image, forward=forward)
        self._template_grad = self._regularise_gradients(first_grad)

        # compute gradient
        # second_grad:  dims x dims x ch x (h x w)
        second_grad = self._calculate_gradients(self._template_grad)
        second_grad = second_grad.pixels.reshape(
            (n_dims, n_dims,  n_channels, -1))

        # Fix crossed derivatives: dydx = dxdy
        second_grad[1, 0, ...] = second_grad[0, 1, ...]

        # compute steepest descent images
        # gradient: dims x dims x ch x (h x w)
        # dw_dp:    dims x           x (h x w) x params
        # sdi:             dims x ch x (h x w) x params
        sdi = 0
        a = second_grad[..., None] * dW_dp[:, None, None, ...]
        for d in a:
            sdi += d

        # reshape steepest descent images
        # sdi: (dims x ch x h x w) x params
        return sdi.reshape((-1, sdi.shape[3]))

    def calculate_hessian(self, sdi):
        # compute hessian
        # sdi.T:   params x (dims x ch x h x w)
        # sdi:              (dims x ch x h x w) x params
        # hessian: params x                     x params
        return sdi.T.dot(sdi)

    def steepest_descent_update(self, sdi, IWxp, template):
        # compute IWxp regularized gradient
        IWxp_grad = self._calculate_gradients(IWxp)
        IWxp_grad = self._regularise_gradients(IWxp_grad)

        # compute vectorized error_image
        # error_img: (dims x ch x h x w)
        self._error_img = (IWxp_grad.as_vector() -
                           self._template_grad.as_vector())

        # compute steepest descent update
        # sdi:       (dims x ch x h x w) x params
        # error_img: (dims x ch x h x w)
        # sdu:                             params
        return sdi.T.dot(self._error_img)


class GradientCorrelation(Residual):

    type = 'GradientCorrelation'

    def steepest_descent_images(self, image, dW_dp, forward=None):
        n_dims = image.n_dims
        n_channels = image.n_channels

        # compute gradient
        # grad:  dims x ch x h x w
        self._grad = self._calculate_gradients(image, forward=forward)
        h, w = self._grad.shape
        grad2 = self._grad.as_vector().reshape(
            (n_dims, n_channels, h, w))

        # compute IGOs (remember axis 0 is y, axis 1 is x)
        # grad:    dims x ch x h x w
        # phi:            ch x h x w
        # cos_phi:        ch x h x w
        # sin_phi:        ch x h x w
        phi = np.angle(grad2[1, ...] + 1j * grad2[0, ...])
        self._cos_phi = np.cos(phi)
        self._sin_phi = np.sin(phi)

        # concatenate sin and cos terms so that we can take the second
        # derivatives correctly. sin(phi) = y and cos(phi) = x which is the
        # correct ordering when multiplying against the warp Jacobian
        # cos_phi:         ch  x h x w
        # sin_phi:         ch  x h x w
        # grad:    (dims x ch) x h x w
        self._grad.from_vector(
            np.concatenate((self._sin_phi, self._cos_phi), axis=0).ravel())

        # compute IGOs gradient
        # second_grad:  dims x dims x ch x (h x w)
        second_grad = self._calculate_gradients(self._grad)
        second_grad = second_grad.pixels.reshape(
            (n_dims, n_dims,  n_channels, -1))

        # Fix crossed derivatives: dydx = dxdy
        second_grad[1, 0, ...] = second_grad[0, 1, ...]

        # reshape cos_phi and sin_phi
        self._cos_phi = self._cos_phi.ravel().reshape((n_channels, -1))
        self._sin_phi = self._sin_phi.ravel().reshape((n_channels, -1))

        # complete full IGOs gradient computation
        # second_grad:  dims x dims x ch x (h x w)
        second_grad[1, ...] = (-self._sin_phi[None, ...] * second_grad[1, ...])
        second_grad[0, ...] = (self._cos_phi[None, ...] * second_grad[0, ...])

        # compute steepest descent images
        # gradient: dims x dims x ch x (h x w)
        # dw_dp:    dims x           x (h x w) x params
        # sdi:                    ch x (h x w) x params
        sdi = 0
        aux = second_grad[..., None] * dW_dp[:, None, None, ...]
        for a in aux.reshape(((-1,) + aux.shape[2:])):
                sdi += a

        # compute constant N
        # N:  1
        self._N = h * w

        # reshape steepest descent images
        # sdi: (ch x h x w) x params
        return sdi.reshape((-1, sdi.shape[2]))

    def calculate_hessian(self, sdi):
        # compute hessian
        # sdi.T:   params x (dims x ch x h x w)
        # sdi:              (dims x ch x h x w) x params
        # hessian: params x                     x params
        return sdi.T.dot(sdi)

    def steepest_descent_update(self, sdi, IWxp, template):
        n_dims = IWxp.n_dims
        n_channels = IWxp.n_channels

        # compute IWxp gradient
        IWxp_grad = self._calculate_gradients(IWxp)
        IWxp_grad = IWxp_grad.as_vector().reshape(
            (n_dims, n_channels, -1))

        # compute IGOs (remember axis 0 is y, axis 1 is x)
        # IWxp_grad:     dims x ch x (h x w)
        # phi:                  ch x (h x w)
        # IWxp_cos_phi:         ch x (h x w)
        # IWxp_sin_phi:         ch x (h x w)
        phi = np.angle(IWxp_grad[1, ...] + 1j * IWxp_grad[0, ...])
        IWxp_cos_phi = np.cos(phi)
        IWxp_sin_phi = np.sin(phi)

        # compute error image
        # error_img:  (ch x h x w)
        self._error_img = (self._cos_phi * IWxp_sin_phi -
                           self._sin_phi * IWxp_cos_phi).ravel()

        # compute steepest descent update
        # sdi:       (ch x h x w) x params
        # error_img: (ch x h x w)
        # sdu:                      params
        sdu = sdi.T.dot(self._error_img)

        # compute step size
        qp = np.sum(self._cos_phi * IWxp_cos_phi +
                    self._sin_phi * IWxp_sin_phi)
        l = self._N / qp
        return l * sdu

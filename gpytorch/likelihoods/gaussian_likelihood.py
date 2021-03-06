#!/usr/bin/env python3

import math
import warnings
from typing import Any, Optional

import torch
from torch import Tensor

from .. import settings
from ..distributions import MultivariateNormal, base_distributions
from ..likelihoods import Likelihood
from ..lazy import ZeroLazyTensor
from .noise_models import FixedGaussianNoise, HomoskedasticNoise, Noise


class _GaussianLikelihoodBase(Likelihood):
    """Base class for Gaussian Likelihoods, supporting general heteroskedastic noise models."""

    def __init__(self, noise_covar: Noise, **kwargs: Any) -> None:

        super().__init__()
        param_transform = kwargs.get("param_transform")
        if param_transform is not None:
            warnings.warn("The 'param_transform' argument is now deprecated. If you want to use a different "
                          "transformaton, specify a different 'noise_constraint' instead.")

        self.noise_covar = noise_covar

    def _shaped_noise_covar(self, base_shape: torch.Size, *params: Any, **kwargs: Any):
        if len(params) > 0:
            # we can infer the shape from the params
            shape = None
        else:
            # here shape[:-1] is the batch shape requested, and shape[-1] is `n`, the number of points
            shape = base_shape
        return self.noise_covar(*params, shape=shape, **kwargs)

    def forward(self, function_samples: Tensor, *params: Any, **kwargs: Any) -> base_distributions.Normal:
        observation_noise = self._shaped_noise_covar(function_samples.shape, *params, **kwargs).diag()
        return base_distributions.Normal(function_samples, observation_noise)

    def marginal(self, function_dist: MultivariateNormal, *params: Any, **kwargs: Any) -> MultivariateNormal:
        mean, covar = function_dist.mean, function_dist.lazy_covariance_matrix
        noise_covar = self._shaped_noise_covar(mean.shape, *params, **kwargs)
        full_covar = covar + noise_covar
        return function_dist.__class__(mean, full_covar)


class GaussianLikelihood(_GaussianLikelihoodBase):
    def __init__(self, noise_prior=None, noise_constraint=None, batch_size=1, **kwargs):
        noise_covar = HomoskedasticNoise(
            noise_prior=noise_prior,
            noise_constraint=noise_constraint,
            batch_size=batch_size,
        )
        super().__init__(noise_covar=noise_covar)

    @property
    def noise(self) -> Tensor:
        return self.noise_covar.noise

    @noise.setter
    def noise(self, value: Tensor) -> None:
        self.noise_covar.initialize(noise=value)

    @property
    def raw_noise(self) -> Tensor:
        return self.noise_covar.raw_noise

    @raw_noise.setter
    def raw_noise(self, value: Tensor) -> None:
        self.noise_covar.initialize(raw_noise=value)

    def expected_log_prob(self, target: Tensor, input: MultivariateNormal, *params: Any, **kwargs: Any) -> Tensor:
        mean, variance = input.mean, input.variance
        noise = self.noise_covar.noise

        if mean.dim() > target.dim():
            target = target.unsqueeze(-1)

        if variance.ndimension() == 1:
            if settings.debug.on() and noise.size(0) > 1:
                raise RuntimeError("With batch_size > 1, expected a batched MultivariateNormal distribution.")
            noise = noise.squeeze(0)

        res = -0.5 * ((target - mean) ** 2 + variance) / noise
        res += -0.5 * noise.log() - 0.5 * math.log(2 * math.pi)
        return res.sum(-1)


class FixedNoiseGaussianLikelihood(_GaussianLikelihoodBase):
    """
    A Likelihood that assumes fixed heteroscedastic noise. This is useful when you have fixed, known observation
    noise for each training example.

    Args:
        :attr:`noise` (Tensor):
            Known observation noise (variance) for each training example.
        :attr:`learn_additional_noise` (bool, optional):
            Set to true if you additionally want to learn added diagonal noise, similar to GaussianLikelihood.

    Note that this likelihood takes an additional argument when you call it, `observation_noise`, that adds
    a specified amount of noise to the passed MultivariateNormal. This allows for adding known observational noise
    to test data.

    Example:
        >>> train_x = torch.randn(55, 2)
        >>> noises = torch.ones(55) * 0.01
        >>> likelihood = FixedNoiseGaussianLikelihood(noise=noises, learn_additional_noise=True)
        >>> pred_y = likelihood(gp_model(train_x))
        >>>
        >>> test_x = torch.randn(21, 2)
        >>> test_noises = torch.ones(21) * 0.02
        >>> pred_y = likelihood(gp_model(test_x), observation_noise=test_noises)
    """
    def __init__(
        self,
        noise: Tensor,
        learn_additional_noise: Optional[bool] = False,
        **kwargs: Any
    ) -> None:
        super().__init__(noise_covar=FixedGaussianNoise(noise=noise))

        if learn_additional_noise:
            noise_prior = kwargs.get("noise_prior", None)
            noise_constraint = kwargs.get("noise_constraint", None)
            batch_size = kwargs.get("batch_size", 1)
            self.second_noise_covar = HomoskedasticNoise(
                noise_prior=noise_prior,
                noise_constraint=noise_constraint,
                batch_size=batch_size,
            )
        else:
            self.second_noise_covar = None

    @property
    def noise(self) -> Tensor:
        return self.noise_covar.noise + self.second_noise

    @noise.setter
    def noise(self, value: Tensor) -> None:
        self.noise_covar.initialize(noise=value)

    @property
    def second_noise(self) -> Tensor:
        if self.second_noise_covar is None:
            return 0
        else:
            return self.second_noise_covar.noise

    @second_noise.setter
    def second_noise(self, value: Tensor) -> None:
        if self.second_noise_covar is None:
            raise RuntimeError(
                "Attempting to set secondary learned noise for FixedNoiseGaussianLikelihood, "
                "but learn_additional_noise must have been False!"
            )
        self.second_noise_covar.initialize(noise=value)

    def _shaped_noise_covar(self, base_shape: torch.Size, *params: Any, **kwargs: Any):
        if len(params) > 0:
            # we can infer the shape from the params
            shape = None
        else:
            # here shape[:-1] is the batch shape requested, and shape[-1] is `n`, the number of points
            shape = base_shape

        res = self.noise_covar(*params, shape=shape, **kwargs)

        if self.second_noise_covar is not None:
            res = res + self.second_noise_covar(*params, shape=shape, **kwargs)
        elif isinstance(res, ZeroLazyTensor):
            warnings.warn(
                "You have passed data through a FixedNoiseGaussianLikelihood that did not match the size "
                "of the fixed noise, *and* you did not specify observation_noise. This is treated as a no-op."
            )

        return res

from warnings import warn
import sys
import time
import pandas as pd
import numpy as np
from nibabel import Nifti1Image

from sklearn.base import BaseEstimator, TransformerMixin, clone
from nilearn._utils.niimg_conversions import check_niimg
from nilearn._utils import CacheMixin
from nilearn.input_data import NiftiMasker
from patsy import DesignInfo

from .first_level_model import FirstLevelModel
from .first_level_model import run_glm
from .regression import OLSModel, SimpleRegressionResults
from .contrasts import compute_contrast
from .utils import _basestring
from .design_matrix import create_second_level_design


class SecondLevelModel(BaseEstimator, TransformerMixin, CacheMixin):
    """ Implementation of the General Linear Model for multiple subject
    fMRI data

    Parameters
    ----------

    mask: Niimg-like, NiftiMasker or MultiNiftiMasker object, optional,
        Mask to be used on data. If an instance of masker is passed,
        then its mask will be used. If no mask is given,
        it will be computed automatically by a MultiNiftiMasker with default
        parameters. Mask automatic computation assumes first level imgs have
        already been masked.

    smoothing_fwhm: float, optional
        If smoothing_fwhm is not None, it gives the size in millimeters of the
        spatial smoothing to apply to the signal.

    memory: string, optional
        Path to the directory used to cache the masking process and the glm
        fit. By default, no caching is done. Creates instance of joblib.Memory.

    memory_level: integer, optional
        Rough estimator of the amount of memory used by caching. Higher value
        means more memory for caching.

    verbose : integer, optional
        Indicate the level of verbosity. By default, nothing is printed.
        If 0 prints nothing. If 1 prints final computation time.
        If 2 prints masker computation details.

    n_jobs : integer, optional
        The number of CPUs to use to do the computation. -1 means
        'all CPUs', -2 'all CPUs but one', and so on.

    """
    def __init__(self, mask=None, smoothing_fwhm=None,
                 memory=None, memory_level=1, verbose=1,
                 n_jobs=1, minimize_memory=True):
        self.mask = mask
        self.smoothing_fwhm = smoothing_fwhm
        self.memory = memory
        self.memory_level = memory_level
        self.verbose = verbose
        self.n_jobs = n_jobs
        self.minimize_memory = minimize_memory
        self.labels_ = None
        self.results_ = None

    def fit(self, second_level_input, first_level_conditions=None,
            regressors=None, design_matrix=None,
            first_level_conditions_name=None):
        """ Fit the second-level GLM

        1. create design matrix
        2. do a masker job: fMRI_data -> Y
        3. fit regression to (Y, X)

        Parameters
        ----------
        second_level_input: list of `FirstLevelModel` objects or pandas
                            DataFrame or list of Niimg-like objects.
            If list of `FirstLevelModel` objects, then first_level_conditions
            must be provided. If a pandas DataFrame, then must contain columns
            model_id, map_name, effects_map_path. If list of Niimg-like
            objects then this is taken literally as Y for the model fit
            and design_matrix must be provided.

        first_level_conditions: list of (str or array) or None
            If second_level_input is a list of `FirstLevelModel` objects then
            it is mandatory to provide a list of contrast definitions to pass
            to the compute_contrast method of `FirstLevelModel`. The contrast
            definitions can be a str or array. Check the compute_contrast
            documentation of `FirstLevelModel` for more details on the
            contrast definitions.

            If second_level_input is a pandas DataFrame then a list of strings
            is expected with the names of maps to include in the model.
            If first_level_conditions is set to None then all maps are included

            If second_level_input is a list of Niimg-like objects then this
            argument is ignored.

        regressors: pandas DataFrame, optional
            Must contain a model_id column. All other columns are
            considered as confounders and included in the model. If
            design_matrix is provided then this argument is ignored.
            The resulting second level design matrix uses the same column
            names as in the given DataFrame for regressors. At least two columns
            are expected, "model_id" and at least one confounder.

        design_matrix: pandas DataFrame, optional
            Design matrix to fit the GLM. The number of rows
            in the design matrix must agree with the size of the list in
            first_level_conditions. Ensure that the order of maps given
            by first_level_conditions or inferred directly from
            second_level_input matches the order of the rows in the design
            matrix.

        first_level_conditions_name: list of str or None,
            This argument only applies when second_level_input is a list of
            `FirstLevelModel` objects. It specifies the name of the columns
            corresponding to the provided first_level_conditions.
            If None then the column names are generated with the form
            'con_00'.
        """
        # Check parameters
        # check first level input
        if isinstance(second_level_input, list):
            if len(second_level_input) < 2:
                raise ValueError('A second level model requires a list with at'
                                 'least two first level models or niimgs')
            # Check FirstLevelModel objects case
            if isinstance(second_level_input[0], FirstLevelModel):
                if first_level_conditions is None:
                    raise ValueError('First level models input requires'
                                     'first_level_conditions to be provided')
                for midx, first_level_model in enumerate(second_level_input):
                    if not isinstance(first_level_model, FirstLevelModel):
                        raise ValueError(' object at idx %d is %s instead of'
                                         ' FirstLevelModel object' %
                                         (midx, type(first_level_model)))
                    if regressors is not None:
                        if first_level_model.model_id is None:
                            raise ValueError(
                                'In case regressors are provided, first level '
                                'objects need to provide the attribute '
                                'model_id to match rows appropriately. Model '
                                'at idx %d do not provide it. To set it, you '
                                'can do first_level_model.model_id = "01"' %
                                (midx))
            # Check niimgs case
            elif isinstance(second_level_input[0], (str, Nifti1Image)):
                if design_matrix is None:
                    raise ValueError('With list of niimgs as second_level_input'
                                     ' a design matrix must be provided')
                for midx, niimg in enumerate(second_level_input):
                    if not isinstance(niimg, (str, Nifti1Image)):
                        raise ValueError(' object at idx %d is %s instead of'
                                         ' Niimg-like object' %
                                         (midx, type(niimg)))
        # Check pandas dataframe case
        elif isinstance(second_level_input, pd.DataFrame):
            for col in ['model_id', 'map_name', 'effects_map_path']:
                if col not in second_level_input.columns:
                    raise ValueError('second_level_input DataFrame must contain'
                                     ' columns model_id, map_name and'
                                     ' effects_map_path')
            if first_level_conditions is not None:
                for cond in first_level_conditions:
                    if not isinstance(cond, str):
                        raise ValueError('When second_level_input is a'
                                         ' DataFrame, first_level_conditions '
                                         'must be a list of str')
        else:
            raise ValueError('second_level_input must be a list of'
                             ' `FirstLevelModel` objects, a pandas DataFrame'
                             ' or a list Niimg-like objects. Instead %s '
                             'was provided' % type(second_level_input))

        # check conditions if provided
        if first_level_conditions is not None:
            if isinstance(first_level_conditions, list):
                for cidx, cond in enumerate(first_level_conditions):
                    if not isinstance(cond, (str, np.ndarray)):
                        raise ValueError('condition at idx %d is %s instead of'
                                         ' str or array' %
                                         (cidx, type(cond)))
            else:
                raise ValueError('first_level_conditions is not a list')

        # check regressors
        if regressors is not None:
            if not isinstance(regressors, pd.DataFrame):
                raise ValueError('regressors must be a pandas DataFrame')
            if 'model_id' not in regressors.columns:
                raise ValueError('regressors DataFrame must contain column'
                                 '"model_id"')
            if len(regressors.columns) < 2:
                raise ValueError('confound should contain at least 2 columns'
                                 'one called "model_id" and the other with'
                                 'a given confounder')

        # check design matrix
        if design_matrix is not None:
            if not isinstance(design_matrix, pd.DataFrame):
                raise ValueError('design matrix must be a pandas DataFrame')

        # check condition names
        if first_level_conditions_name is not None:
            if not isinstance(first_level_conditions_name, (list, tuple)):
                raise ValueError('first_level_conditions_name must be a '
                                 'list of strings')
            if len(first_level_conditions_name) != len(first_level_conditions):
                raise ValueError('length of first_level_conditions_name and'
                                 ' first_level_conditions must match')
            for name in first_level_conditions_name:
                if not isinstance(name, str):
                    raise ValueError('first_level_conditions_name must be a '
                                     'list of strings')

        # Build the design matrix X and list of imgs Y for GLM fit
        if isinstance(second_level_input, pd.DataFrame):
            maps_table = second_level_input
            # Get only first level conditions if provided
            if first_level_conditions is not None:
                for condition in first_level_conditions:
                    if condition not in maps_table['map_name'].tolist():
                        raise ValueError('condition %s not present in'
                                         ' second_level_input' % condition)
                in_cond = maps_table.apply(
                    lambda x: x['map_name'] in first_level_conditions, axis=1)
                maps_table = maps_table[in_cond]
            # Create design matrix if necessary
            if design_matrix is None:
                design_matrix = create_second_level_design(maps_table,
                                                           regressors)
            # get effect maps for fixed effects GLM
            effects_maps = maps_table['effects_map_path'].tolist()

        elif isinstance(second_level_input[0], FirstLevelModel):
            # Check models were fit
            for midx, model in enumerate(second_level_input):
                if model.labels_ is None:
                    raise ValueError('Model at idx %d has not been fit' % midx)
            # Get the first level model maps
            maps_table = pd.DataFrame(columns=['map_name', 'model_id'])
            effects_maps = []
            for model in second_level_input:
                for con_idx, con_def in enumerate(first_level_conditions):
                    con_name = 'con_%02d' % con_idx
                    if first_level_conditions_name:
                        con_name = first_level_conditions_name[con_idx]
                    maps_table.loc[len(maps_table)] = [con_name,
                                                     model.model_id]
                    eff_map = model.compute_contrast(con_def,
                                                     output_type='effect_size')
                    effects_maps.append(eff_map)
            # Get the design matrix
            if design_matrix is None:
                design_matrix = create_second_level_design(maps_table,
                                                           regressors)

        else:
            effects_maps = second_level_input

        # set design matrix, given or computed
        self.design_matrix_ = design_matrix

        # check design matrix X and effect maps Y agree on number of rows
        if len(effects_maps) != design_matrix.shape[0]:
            raise ValueError('design_matrix X number of rows do not agree with'
                             ' number of maps Y considered')
        # check niimgs
        for niimg in effects_maps:
            check_niimg(niimg, ensure_ndim=3)

        # Report progress
        t0 = time.time()
        if self.verbose > 0:
            sys.stderr.write("Computing second level model. "
                             "Go take a coffee\r")

        # Learn the mask. Assume the first level imgs have been masked.
        if not isinstance(self.mask, NiftiMasker):
            self.masker_ = NiftiMasker(
                mask_img=self.mask, smoothing_fwhm=self.smoothing_fwhm,
                memory=self.memory, verbose=max(0, self.verbose - 1),
                memory_level=self.memory_level)
        else:
            self.masker_ = clone(self.mask)
            for param_name in ['smoothing_fwhm', 'memory', 'memory_level']:
                our_param = getattr(self, param_name)
                if our_param is None:
                    continue
                if getattr(self.masker_, param_name) is not None:
                    warn('Parameter %s of the masker overriden' % param_name)
                setattr(self.masker_, param_name, our_param)
        self.masker_.fit(effects_maps[0])

        # Fit the model
        Y = self.masker_.transform(effects_maps)
        if self.memory is not None:
            arg_ignore = ['n_jobs', 'noise_model']
            mem_glm = self.memory.cache(run_glm, ignore=arg_ignore)
        else:
            mem_glm = run_glm
        labels, results = mem_glm(Y, design_matrix.as_matrix(),
                                  n_jobs=self.n_jobs, noise_model='ols')
        # We save memory if inspecting model details is not necessary
        if self.minimize_memory:
            for key in results:
                results[key] = SimpleRegressionResults(results[key])
        self.labels_ = labels
        self.results_ = results
        del Y

        # Report progress
        if self.verbose > 0:
            sys.stderr.write("\nComputation of second level model done in "
                             "%i seconds\n" % (time.time() - t0))

        return self

    def compute_contrast(self, contrast_def, stat_type=None,
                         output_type='z_score'):
        """Generate different outputs corresponding to
        the contrasts provided e.g. z_map, t_map, effects and variance.

        Parameters
        ----------
        contrast_def : str or array of shape (n_col)
            where ``n_col`` is the number of columns of the design matrix,
            The string can be a formula compatible with the linear constraint
            of the Patsy library. Basically one can use the name of the 
            conditions as they appear in the design matrix of
            the fitted model combined with operators /*+- and numbers.
            Please checks the patsy documentation for formula examples:
            http://patsy.readthedocs.io/en/latest/API-reference.html#patsy.DesignInfo.linear_constraint

        stat_type : {'t', 'F'}, optional
            type of the contrast

        output_type : str, optional
            Type of the output map. Can be 'z_score', 'stat', 'p_value',
            'effect_size' or 'effect_variance'

        Returns
        -------
        output_image : Nifti1Image
            The desired output image

        """
        # check model was fit
        if self.labels_ is None or self.results_ is None:
            raise ValueError('The model has not been fit yet')

        # check contrast definition
        if isinstance(contrast_def, np.ndarray):
            con_val = contrast_def
            if np.all(con_val == 0):
                raise ValueError('Contrast is null')
        else:
            design_info = DesignInfo(self.design_matrix_.columns.tolist())
            con_val = design_info.linear_constraint(contrast_def).coefs

        # check output type
        if isinstance(output_type, _basestring):
            if output_type not in ['z_score', 'stat', 'p_value', 'effect_size',
                                   'effect_variance']:
                raise ValueError('output_type must be one of "z_score", "stat"'
                                 ', "p_value", "effect_size" or "effect_variance"')
        else:
            raise ValueError('output_type must be one of "z_score", "stat",'
                             ' "p_value", "effect_size" or "effect_variance"')

        if self.memory is not None:
            arg_ignore = ['labels', 'results']
            mem_contrast = self.memory.cache(compute_contrast,
                                             ignore=arg_ignore)
        else:
            mem_contrast = compute_contrast
        contrast = mem_contrast(self.labels_, self.results_, con_val,
                                stat_type)

        estimate_ = getattr(contrast, output_type)()
        # Prepare the returned images
        output = self.masker_.inverse_transform(estimate_)
        contrast_name = str(con_val)
        output.get_header()['descrip'] = (
            '%s of contrast %s' % (output_type, contrast_name))
        return output

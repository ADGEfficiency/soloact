import sox
import random
import yaml
import os
import numpy as np
from inspect import getmembers, signature, isclass, isfunction, ismethod
import librosa
import librosa.display
import yaml
import tempfile
import glob
import logging
import pandas as pd
import itertools
import sys
from collections import OrderedDict

import soloact

random.seed(666) # seed is set for the (hidden) global Random()

def flatten(x, par = '', sep ='.'):
    """
    Recursively flatten dictionary with parent key separator
    Use to flatten augmentation labels in DataFrame output

    Args:
        x (dict): with nested dictionaries.
        par (str): parent key placeholder for subsequent levels from root
        sep (str: '.', '|', etc): separator

    Returns:
        Flattened dictionary

    Example:
        x = {'Cookies' : {'milk' : 'Yes', 'beer' : 'No'}}
        par, sep = <defaults>
        output = {'Cookies.milk' : 'Yes', 'Cookies.beer' : 'No'}

    """

    store = {}
    for k,v in x.items():
        if isinstance(v,dict):
            store = {**store, **flatten(v, par =  par + sep + k if par else k)}
        else:
            store[par + sep + k] = v
    return store

def load_track(path, sr = 44100):

    """Librosa load to numpy array

    Args:
        path (str): filepath
        sr (int): sampling rate - 44100 default and preferred

    """

    x, sr = librosa.load(path, sr = sr) # default
    return x

def rand(x,y):
    """

    Randomizer for augmentation pipepline

    Args:
    x (int, float): lower_bound
    y (int, float): upper_bound

    Returns:
        random number between bounds

    """

    # use uniform if parameters passed are below 1
    if all([v < 1 for v in [x,y]]):
        return random.uniform(x, y)
    else:
        return random.randint(x,y)

def validate_reduce_fx(effects):

    """
    Function crossvalidating existence of effects
    between pysox and configuration

    Args:
        effects (dict): desired effects for augmentation


    Returns:
        Bool & dict
            True - all effects present, return original effects dictionary
            False - one or more not present, return effects not in pysox

    """

    FX = sox.Transformer()
    sox_arsenal = dict(getmembers(FX, predicate=lambda x: ismethod(x)))
    try:
        assert all(f in sox_arsenal for f in effects), 'Invalid methods provided'
        return True, effects
    except Exception as e:
        invalid = {f for f in effects if f not in sox_arsenal}
        return False, invalid


logger = logging.getLogger()
logger.setLevel('CRITICAL')

def feature_pipeline(arr, **kwargs):

    """
    Current feature pipeline supporting mfcc only

    Args:
        arr(np array): row vector generated by librosa.load
        kwargs (dict): arguments to feature.mfcc

    Returns:
        vector of shape (n_mfcc, )

    """

    mfcc = librosa.feature.mfcc(arr,
            sr = 44100,
            n_mfcc = 26, **kwargs)
    mfcc_mean = np.mean(mfcc, axis = 0)
    return mfcc_mean


def pad(l_arrays):

    """
    Naive padding using max shape from list of numpy arrays
    to normalize shapes for all

    Args:
        l_array (list of np arrays):

    Returns:
        np.matrix of shape (length of array, features, 1)

    """
    # Retrieve max
    max_shape = max([x.shape for x in l_arrays], key = lambda x: x[0])

    def padder(inp, max_shape):
        zero_grid = np.zeros(max_shape)
        x,y = inp.shape
        zero_grid[:x, :y] = inp
        return zero_grid

    # Pad with zero grid skeleton
    reshapen = [padder(x, max_shape) for x in l_arrays]

    # Make ndarray
    batch_x = np.array(reshapen)

    return batch_x


def augment_track(file, n, effects,
               exercise = 'regression',
               sustain = ['overdrive', 'reverb'],
               write = False
              ):
    """
    Track-wise augmentation procedure

    - Classification: randomize effect on or off - no randomization at parameter level
    - Regression: Persistent effects with randomization at parameter level
                  dependent on config state ('default', 'random')

    Args:
        file (str): filepath to .wav file
        effects (dict): candidate effects defined by config state
            state (str):
                'random' : requires upper and lower bounds
                'constant': will take upper if default is False, otherwise effect default
        exercise (str): regression or classification
        sustain: effect to persist despite randomized on/off in classification
        write (bool: False, str: Path):
            Path: if directory not present will make

    """

    # Init transformer
    FX = sox.Transformer()

    labels = {}

    for effect, parameters in effects.items():

        if exercise.lower() == 'classification' and effect not in sustain:

            # turn effects on or off randomly
            if int(random.choice([True, False])) == 0:
                # print ('{} skipped!'.format(effect))
                continue # skip effect

        effect_f = getattr(FX, effect)
        f_defaults = signature(effect_f).parameters

        # store defaults, could be done out of scope
        f_defaults = {k: f_defaults[k].default for k in f_defaults.keys()}

        used = {}

        for param, val in parameters.items():

            state = val.get('state')
            default = val.get('default') # boolean whether to use default or not

            if state == 'constant':
                used[param] = f_defaults.get(param) if default is True else val.get('upper') # upper can be a list
            elif state == 'random':
                # retrieve bounds
                if not isinstance(f_defaults.get(param), list):
                    lower, upper = [val.get(bound) for bound in ['lower', 'upper']]
                    assert upper > lower, \
                       'Upper bound for {} must be greater than its lower bound'.format(effect + '.' + param)
                    used[param] = rand(lower, upper)
                    continue
                raise TypeError('Will not parse random list values!')
        effect_f(**used)
        labels[effect] = used

    if write is not False:
        # outputs augmented tracks to desired folder ignoring feature extraction pipleine
        model = file.split('/')[-2] if '/audio/' not in file else file.split('/')[-3]
        outfile = os.path.join(write, model, str(n) + '_' + file.split('/')[-1])
        # print (outfile)
        FX.build(file, outfile)

    with tempfile.NamedTemporaryFile(suffix = '.wav') as tmp:
        # pysox doesn't have output to array, save to temp file and reload as array with librosa
        FX.build(file, tmp.name)
        array, sr = librosa.load(tmp.name, sr = 41000)
        FX.clear_effects()
        # return data with feature extraction
        flattened_labels = flatten(labels)
        flattened_labels['group'] = n
        return flattened_labels, feature_pipeline(array)

def augment_data(SOURCES, subsample = False, n_augment = 1,
                 write_with_effects_to = False, make_training_set = False, source = 'power'):
    """
    Augmentation pipeline with options to subsample or write augmented data

    Args:
        subsample (bool, int): if not False augment only k files
        write_with_effects (bool, str: path): if not False write augmented .wav files without feature extraction
        write_training (bool):
            True -> write to data/processed folder with feature extraction (intended to mimic pipleine structure)
            False -> return ndarray of features and dataframe of labels
        n_augment(int): number to augment per file

    Returns:
        if no write_with_effects path provided:
            ndarray of features and dataframe of labels
        otherwise:
            files written to provided directory

    """

    # SOURCES = soloact.make_source_paths()

    TRACK_KIND = SOURCES[source] # note or chord
    SOURCE_DIR = TRACK_KIND['trace']

    # must be a yaml file
    # config = 'config.yaml' if config is None else config
    config = yaml.load(open(SOURCES['config'], 'r'))

    # SPLIT CONFIGURATIONS
    augmentation_config = config['DataAugmentation']

    pipeline_config = config['pipeline_config']

    # PREDETERMINED MODEL GUITAR SPLIT
    use_models_train = pipeline_config['train_models']
    # NOT GENERATING THIS, HOLDING OUT UNTIL WE HAVE USEFUL WORKING MODELS
    use_models_test = pipeline_config['test_models']

    train_soundfiles = [glob.glob(os.path.join(SOURCE_DIR, mod, TRACK_KIND.get('ext')) + '/*.wav')
                        for mod in use_models_train]
    train_soundfiles = list(itertools.chain.from_iterable(train_soundfiles))
#
    if subsample is not False:
        print ('Subsampling {} files from {} available'.format(subsample, len(train_soundfiles)))
        train_soundfiles = random.choices(population = train_soundfiles, k = subsample)
    else:
        print ('Using all available data, {} files'.format(len(train_soundfiles)))

    # VALIDATE EFFECTS BEFORE STARTING AUGMENTATION CHAIN
    effects = augmentation_config.get('effects')

    # Independent of the next step
    valid, effects = validate_reduce_fx(effects)

    # REDUCE LIST TO ACTIVE ONLY
    effects = {k:v for k,v in effects.items() if k in augmentation_config.get('active')}

    # NOT A KEYWORD TO AUGMENTATION FUNCTION
    augmentation_config.pop('active')

    # FIXED ORDER
    order = ['overdrive'] + [f for f in effects.keys() if f not in ['reverb', 'overdrive']] + ['reverb']
    ordered_effects = OrderedDict.fromkeys(order)

    # ADD EFFECTS BACK TO ORDERED DICT
    for k,v in effects.items():
        ordered_effects[k] = v

    # REPLACE
    augmentation_config['effects'] = ordered_effects

    if write_with_effects_to:

        OUT_DIR = os.path.join(INTERIM_DIR, write_with_effects_to) + '_' + source.upper()

        print ('Are you sure you want to {} files to "{}"?'.format(
                len(train_soundfiles) * n_augment, OUT_DIR))

        print ('1 to proceed, any other key to terminate')

        if int(input()) == 1:
            # can't take relative path with join here
            augmentation_config['write'] = OUT_DIR
            for m in use_models_train:
                # make directories for each model!
                os.makedirs(os.path.dirname(os.path.join(OUT_DIR, m) + '/'), exist_ok=True)
        else:
            sys.exit('Operation cancelled')

    store_all = []

    for sf in train_soundfiles:
        for i in range(n_augment):
            store_all.append(augment_track(sf, n = i, **augmentation_config))

    labels, features = zip(*store_all)
    all_features = [np.expand_dims(x, axis = 1) for x in features]
    X_train = pad(all_features)
    Y_train = pd.DataFrame(list(labels))
    # add guitar kind and chord
    def gt(x, ix): return x.split('/')[ix]

    # get models and chords (ordered)
    file_meta = [(gt(x, ix = -2 if source != 'sn' else -3), gt(x, ix = -1).rstrip('.wav')) for x in train_soundfiles]
    models, chordnames = zip(*file_meta)
    # repeat sequence by number of augmentations
    models = list(itertools.chain.from_iterable([[m] * n_augment for m in models]))
    chordnames = list(itertools.chain.from_iterable([[m] * n_augment for m in chordnames]))
    Y_train['model'] = pd.Series(models)
    Y_train['chords'] = pd.Series(chordnames)

    if make_training_set is True:
        processed_dir = os.path.abspath(SOURCES['DATA_DIR']) + '/processed/'
        # OVERWRITES EXISTING TRAINING DATA
        np.save(processed_dir + 'training_X_' + source, arr =  X_train)
        Y_train.to_csv(open(processed_dir + 'training_Y_' + source  + '.csv', 'w'))
        print ('Wrote training data to "{}"'.format(processed_dir))
    #
    return X_train, Y_train
#

if __name__ == '__main__':
    X_train, y_train = augment_data(source = 'power', make_training_set =  True, n_augment = 1, subsample = 5, config = 'config.yaml')

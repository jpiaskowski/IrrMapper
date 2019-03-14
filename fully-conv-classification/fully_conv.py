import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
import time
import keras.backend as K
import tensorflow as tf
tf.enable_eager_execution()
import matplotlib.pyplot as plt
import numpy as np
import json
import geopandas as gpd
import sys
from glob import glob
from skimage import transform, util
from sklearn.metrics import confusion_matrix
from tensorflow.keras.callbacks import TensorBoard
from rasterio import open as rasopen
from rasterio.mask import mask
from shapely.geometry import shape
from fiona import open as fopen
from data_generators import generate_training_data, load_raster, preprocess_data
from data_utils import generate_class_mask
from models import fcnn_functional, fcnn_model, fcnn_functional_small, unet

NO_DATA = -1
CHUNK_SIZE = 572 # some value that is divisible by 2^MAX_POOLS.
NUM_CLASSES = 5
WRS2 = '../spatial_data/wrs2_descending_usa.shp'

def custom_objective(y_true, y_pred):
    y_true_for_loss = y_true
    mask = tf.not_equal(y_true, NO_DATA)
    y_true_for_loss = tf.where(mask, y_true, tf.zeros_like(y_true))
    y_true_for_loss = tf.cast(y_true_for_loss, tf.int32)
    losses = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=y_pred, labels=y_true_for_loss)
    # the above line works in eager mode, but not otherwise.
    # losses = tf.keras.losses.sparse_categorical_crossentropy(y_true_for_loss, y_pred)
    out = tf.boolean_mask(losses, mask)
    return out

def weighted_loss(weight_map):
    # All I need to do is multiply the output loss
    # by the weights that I input. 
    # Loss is of shape n_classesxwidthxheight
    # what does weight map have to be in this case?
    # 
    def loss(y_true, y_pred):
        losses = 0
        pass
    pass


def custom_objective_v2(y_true, y_pred):
    '''I want to mask all values that 
       are not data, given a y_true 
       that has NODATA values. The boolean mask 
       operation is failing. It should output
       a Tensor of shape (M, N_CLASSES), but instead outputs a (M, )
       tensor.'''
    y_true = tf.reshape(y_true, (K.shape(y_true)[1]*K.shape(y_true)[2], NUM_CLASSES))
    y_pred = tf.reshape(y_pred, (K.shape(y_pred)[1]*K.shape(y_pred)[2], NUM_CLASSES))
    masked = tf.not_equal(y_true, NO_DATA)
    indices = tf.where(masked)
    indices = tf.to_int32(indices)
    indices = tf.slice(indices, [0, 0], [K.shape(indices)[0], 1])
    y_true_masked = tf.gather_nd(params=y_true, indices=indices)
    y_pred_masked = tf.gather_nd(params=y_pred, indices=indices)
    return tf.keras.losses.categorical_crossentropy(y_true_masked, y_pred_masked)


def masked_acc(y_true, y_pred):
    y_pred = tf.nn.softmax(y_pred)
    y_pred = tf.argmax(y_pred, axis=3)
    mask = tf.not_equal(y_true, NO_DATA)
    y_true = tf.boolean_mask(y_true, mask)
    y_pred = tf.boolean_mask(y_pred, mask)
    y_true = tf.cast(y_true, tf.int64)
    y_pred = tf.cast(y_pred, tf.int64)
    return K.mean(tf.math.equal(y_true, y_pred))


def m_acc(y_true, y_pred):
    ''' Calculate accuracy from masked data.
    The built-in accuracy metric uses all data (masked & unmasked).'''
    y_true = tf.reshape(y_true, (K.shape(y_true)[1]*K.shape(y_true)[2], NUM_CLASSES))
    y_pred = tf.reshape(y_pred, (K.shape(y_pred)[1]*K.shape(y_pred)[2], NUM_CLASSES))
    masked = tf.not_equal(y_true, NO_DATA)
    indices = tf.where(masked)
    indices = tf.to_int32(indices)
    indices = tf.slice(indices, [0, 0], [K.shape(indices)[0], 1])
    y_true_masked = tf.gather_nd(params=y_true, indices=indices)
    y_pred_masked = tf.gather_nd(params=y_pred, indices=indices)
    return K.cast(K.equal(K.argmax(y_true_masked, axis=-1), K.argmax(y_pred_masked, axis=-1)), K.floatx())


def evaluate_image_unet(master_raster, model, max_pools, outfile=None, ii=None):

    if not os.path.isfile(master_raster):
        print("Master raster not created for {}".format(suffix))
        # TODO: More extensive handling of this case.
    else:
        master, meta = load_raster(master_raster)
        class_mask = np.zeros((2, master.shape[1], master.shape[2])) # Just a placeholder
        out = np.zeros((master.shape[2], master.shape[1], NUM_CLASSES))

        CHUNK_SIZE = 572
        diff = 92
        stride = 388

        for i in range(0, master.shape[1]-diff, stride):
            for j in range(0, master.shape[2]-diff, stride):
                sub_master = master[:, i:i+CHUNK_SIZE, j:j+CHUNK_SIZE]
                sub_mask = class_mask[:, i:i+CHUNK_SIZE, j:j+CHUNK_SIZE]
                sub_master, sub_mask, cut_rows, cut_cols = preprocess_data(sub_master, sub_mask,
                        max_pools, return_cuts=True)
                if sub_master.shape[1] == 572 and sub_master.shape[2] == 572:
                    preds = model.predict(sub_master)
                    preds_exp = np.exp(preds)
                    preds_softmaxed = preds_exp / np.sum(preds_exp, axis=3, keepdims=True)
                    if np.any(np.isnan(preds)):
                        print("Nan prediction.")
                    preds = preds_softmaxed[0, :, :, :]
                else:
                    continue

                if cut_cols == 0 and cut_rows == 0:
                    out[j+diff:j+CHUNK_SIZE-diff, i+diff:i+CHUNK_SIZE-diff, :] = preds
                elif cut_cols == 0 and cut_rows != 0:
                    ofs = master.shape[1]-cut_rows
                    out[j+diff:j+CHUNK_SIZE-diff, i+diff:ofs-diff, :] = preds
                elif cut_cols != 0 and cut_rows == 0:
                    ofs = master.shape[2]-cut_cols
                    out[j+diff:ofs-diff, i+diff:i+CHUNK_SIZE-diff, :] = preds
                elif cut_cols != 0 and cut_rows != 0:
                    ofs_col = master.shape[2]-cut_cols
                    ofs_row = master.shape[1]-cut_rows
                    out[j+diff:ofs_col-diff, i+diff:ofs_row-diff, :] = preds
                else:
                    print("whatcha got goin on here?")

            sys.stdout.write("N eval: {}. Percent done: {:.4f}\r".format(ii, i / master.shape[1]))

    out = np.swapaxes(out, 0, 2)
    out = out.astype(np.float32)
    if outfile:
        save_raster(out, outfile, meta)
    return out
def evaluate_image(master_raster, model, max_pools, outfile=None, ii=None):

    if not os.path.isfile(master_raster):
        print("Master raster not created for {}".format(suffix))
        # TODO: More extensive handling of this case.
    else:
        master, meta = load_raster(master_raster)
        class_mask = np.zeros((2, master.shape[1], master.shape[2])) # Just a placeholder
        out = np.zeros((master.shape[2], master.shape[1], NUM_CLASSES))

        CHUNK_SIZE = 572

        for i in range(0, master.shape[1], CHUNK_SIZE):
            for j in range(0, master.shape[2], CHUNK_SIZE):
                sub_master = master[:, i:i+CHUNK_SIZE, j:j+CHUNK_SIZE]
                sub_mask = class_mask[:, i:i+CHUNK_SIZE, j:j+CHUNK_SIZE]
                sub_master, sub_mask, cut_rows, cut_cols = preprocess_data(sub_master, sub_mask,
                        max_pools, return_cuts=True)
                if sub_master.shape[1] == 572 and sub_master.shape[2] == 572:
                    preds = model.predict(sub_master)
                    preds_exp = np.exp(preds)
                    preds_softmaxed = preds_exp / np.sum(preds_exp, axis=3, keepdims=True)
                    if np.any(np.isnan(preds)):
                        print("Nan prediction.")
                    preds = preds_softmaxed[0, :, :, :]
                else:
                    continue
                oss = 92
                if cut_cols == 0 and cut_rows == 0:
                    out[j+oss:j+CHUNK_SIZE-oss, i+oss:i+CHUNK_SIZE-oss, :] = preds
                elif cut_cols == 0 and cut_rows != 0:
                    ofs = master.shape[1]-cut_rows
                    out[j+oss:j+CHUNK_SIZE-oss, i+oss:ofs-oss, :] = preds
                elif cut_cols != 0 and cut_rows == 0:
                    ofs = master.shape[2]-cut_cols
                    out[j+oss:ofs-oss, i+oss:i+CHUNK_SIZE-oss, :] = preds
                elif cut_cols != 0 and cut_rows != 0:
                    ofs_col = master.shape[2]-cut_cols
                    ofs_row = master.shape[1]-cut_rows
                    out[j+oss:ofs_col-oss, i+oss:ofs_row-oss, :] = preds
                else:
                    print("whatcha got goin on here?")

            sys.stdout.write("N eval: {}. Percent done: {:.4f}\r".format(ii, i / master.shape[1]))

    out = np.swapaxes(out, 0, 2)
    out = out.astype(np.float32)
    if outfile:
        save_raster(out, outfile, meta)
    return out

def save_raster(arr, outfile, meta, count=NUM_CLASSES):
    meta.update(count=count)
    with rasopen(outfile, 'w', **meta) as dst:
        dst.write(arr)


def get_features(gdf, path, row):
    tmp = json.loads(gdf.to_json())
    features = []
    for feature in tmp['features']:
        if feature['properties']['PATH'] == path and feature['properties']['ROW'] == row: 
            features.append(feature['geometry'])
    return features

def clip_raster(evaluated, path, row, outfile=None):

    shp = gpd.read_file(WRS2)

    with rasopen(evaluated, 'r') as src:
        shp = shp.to_crs(src.crs)
        meta = src.meta.copy()
        features = get_features(shp, path, row)
        out_image, out_transform = mask(src, shapes=features, nodata=np.nan)

    if outfile:
        save_raster(out_image, outfile, meta)

def clip_rasters(evaluated_tif_dir, include_string):
    for f in glob(os.path.join(evaluated_tif_dir, "*.tif")):
        if include_string in f:
            out = os.path.basename(f)
            out = out[out.find("_")+1:]
            out = out[out.find("_")+1:]
            out = out[out.find("_")+1:]
            path = out[:2]
            row = out[3:5]
            clip_raster(f, int(path), int(row), outfile=f)

def evaluate_images(image_directory, model, include_string, max_pools, exclude_string, prefix, save_dir):
    ii = 0
    for f in glob(os.path.join(image_directory, "*.tif")):
        if exclude_string not in f and include_string in f:
            print(f)
            out = os.path.basename(f)
            os.path.split(out)[1]
            out = out[out.find("_"):]
            out = os.path.splitext(out)[0]
            out = prefix + out + ".tif"
            out = os.path.join(save_dir, out)
            ii += 1
            evaluate_image_unet(f, model, max_pools=max_pools, outfile=out, ii=ii)

def compute_iou(y_pred, y_true):
     ''' This is slow. '''
     y_pred = y_pred.flatten()
     y_true = y_true.flatten()
     current = confusion_matrix(y_true, y_pred, labels=[0, 1, 2, 3])
     print(current)
     # compute mean iou
     intersection = np.diag(current)
     ground_truth_set = current.sum(axis=1)
     predicted_set = current.sum(axis=0)
     union = ground_truth_set + predicted_set - intersection
     IoU = intersection / union.astype(np.float32)
     return np.mean(IoU)

def get_iou():
    shpfiles = [
    'shapefile_data/test/MT_Huntley_Main_2013_372837_28.shp',
    'shapefile_data/test/MT_FLU_2017_Fallow_372837_28.shp',
    'shapefile_data/test/MT_FLU_2017_Forrest_372837_28.shp',
    'shapefile_data/test/MT_other_372837_28.shp']

    m_dir = 'eval_test/all_ims/'
    ls = []
    mask = image_directory + 'class_mask_37_28_2013.tif'
    for f in shpfiles:
        msk = generate_class_mask(f, mask)
        msk[msk != NO_DATA] = 1 
        ls.append(msk)
    y_true = np.vstack(ls)
    indices = np.where(y_true != NO_DATA)
    y_true = y_true[:, indices[1], indices[2]]
    y_true = np.argmax(y_true, axis=0)
    for f in glob(m_dir + "*.tif"):
        y_pred, meta = load_raster(f)
        y_pred = y_pred[:, indices[1], indices[2]]
        y_pred = np.round(y_pred)
        y_pred.astype(np.int32)
        y_pred = np.argmax(y_pred, axis=0)
        print(f, compute_iou(y_pred, y_true))

def train_model(training_directory, model, steps_per_epoch, valid_steps, max_pools, box_size=0,
        epochs=3, random_sample=False, restore=False, learning_rate=1e-3):
    ''' This function assumes that train/test data are
    subdirectories of training_directory, with
    the names train/test.'''
    if not restore:
        model = model(NUM_CLASSES)
    if NUM_CLASSES <= 2:
        model.compile(loss=custom_objective_binary,
                     metrics=[m_acc],
                     optimizer='adam')
    else:
        # model.compile(
        #          loss=custom_objective,
        #          optimizer='adam', 
        #          metrics=[masked_acc]
        #          )
        model.compile(
                 loss=custom_objective,
                 optimizer=tf.train.AdamOptimizer(learning_rate=learning_rate),
                 metrics=[masked_acc]
                 )
    graph_path = os.path.join('graphs/', str(int(time.time())))
    os.mkdir(graph_path)
    tb = TensorBoard(log_dir=graph_path, write_images=True, batch_size=4)
    train = os.path.join(training_directory, 'train')
    test = os.path.join(training_directory, 'test')
    train_generator = generate_training_data(train, max_pools, sample_random=random_sample,
            box_size=box_size)
    test_generator = generate_training_data(test, max_pools, sample_random=random_sample,
            box_size=box_size)
    model.fit_generator(train_generator, 
            validation_data=test_generator,
            validation_steps=valid_steps,
            steps_per_epoch=steps_per_epoch, 
            epochs=epochs,
            callbacks=[tb, tf.keras.callbacks.TerminateOnNaN()],
            verbose=1,
            class_weight=[25.923, 1.0, 2.79, 61.128, .75],
            use_multiprocessing=True)

    return model, graph_path


def save_model_info(outfile, args):
    template = '{}={}|'
    with open(outfile, 'a') as f:
        for key in args:
            f.write(template.format(key, args[key]))
        f.write("\n-------------------\n")
    print("wrote run info to {}".format(outfile))


if __name__ == '__main__':

    training_directory = 'training_data/multiclass/'
    info_file = 'run_information.txt'

    max_pools = 0
    model_name = 'unet_{}.h5'.format(int(time.time()))
    #model_name = 'unet_random_sample100.h5'
    model_dir = 'models/'
    info_path = os.path.join(model_dir, info_file)
    model_save_path = os.path.join(model_dir, model_name)

    model_func = unet

    steps_per_epoch = 157 #628
    valid_steps = 1 #233
    epochs = 1

    train_more = False
    eager = True
    class_weights = True
    learning_rate = 1e-4
    random_sample = False
    augment = False

    raster_name = '5class'
    pr_to_eval = '39_27'
    image_directory = 'master_rasters/train/'

    param_dict = {'model_name':model_name, 'epochs':epochs, 'steps_per_epoch':steps_per_epoch,
            'raster_name':raster_name, 'learning_rate':learning_rate, 'eager':eager,
            'class_weights':class_weights, 'augmented':augment, 'random_sample':random_sample, 'graph_path':None}

    evaluating = True
    if not os.path.isfile(model_save_path):
        model, graph_path = train_model(training_directory, model_func,
                steps_per_epoch=steps_per_epoch, valid_steps=valid_steps,
                max_pools=max_pools, epochs=epochs,
                random_sample=random_sample, learning_rate=learning_rate)
        evaluating = False
        model.save(model_save_path)
    else:
        model = tf.keras.models.load_model(model_save_path,
                custom_objects={'custom_objective':custom_objective})
        if train_more:
            model, graph_path = train_model(training_directory, model, steps_per_epoch=steps_per_epoch,
                    valid_steps=valid_steps, random_sample=random_sample,
                    max_pools=max_pools, epochs=epochs, restore=True)
            model_name = 'unet_random_sample100.h5'
            model.save(os.path.join(model_dir, model_name))

    if not evaluating or train_more:
        param_dict['graph_path'] = graph_path
        save_model_info(info_path, param_dict)
    
    evaluate_images(image_directory, model, include_string=pr_to_eval, 
             exclude_string="class", max_pools=max_pools, prefix=raster_name,
             save_dir='compare_model_outputs/blurry/') 
    #clip_rasters('compare_model_outputs/blurry/', pr_to_eval)

# Databricks notebook source
import numpy as np
import tensorflow as tf
import horovod.tensorflow as hvd

from pyspark.sql.types import *
from pyspark.sql.functions import rand, when

from sparkdl.estimators.horovod_estimator.estimator import HorovodEstimator

# COMMAND ----------

# Load MNIST dataset, with images represented as arrays of floats
(x_train, y_train), (x_test, y_test) = tf.keras.datasets.mnist.load_data("/tmp/mnist")
x_train = x_train.reshape((x_train.shape[0], -1))
data = [(x_train[i].astype(float).tolist(), int(y_train[i])) for i in range(len(y_train))]
schema = StructType([StructField("image", ArrayType(FloatType())),
                     StructField("label_col", LongType())])
df = spark.createDataFrame(data, schema)
display(df)

# COMMAND ----------

help(HorovodEstimator)

# COMMAND ----------

def model_fn(features, labels, mode, params):
    """
    Arguments:
        * features: Dict of DataFrame input column name to tensor (each tensor corresponding to
                    batch of data from the input column)
        * labels: Tensor, batch of labels
        * mode: Specifies if the estimator is being run for training, evaluation or prediction.
        * params: Optional dict of hyperparameters. Will receive what is passed to
                  HorovodEstimator in params parameter. This allows for configuring Estimators for
                  hyperparameter tuning.
    Returns: tf.estimator.EstimatorSpec describing our model.  
    """
    from tensorflow.examples.tutorials.mnist import mnist
    # HorovodEstimator feeds scalar Spark SQL types to model_fn as tensors of shape [None]
    # (i.e. a variable-sized batch of scalars), and array Spark SQL types (including 
    # VectorUDT) as tensors of shape [None, None] (i.e. a variable-sized batch of dense variable-length arrays).
    #
    # Here image data is fed from an ArrayType(FloatType()) column,
    # e.g. as a float tensor with shape [None, None]. We know each float array is of length 784,
    # so we reshape our tensor into one of shape [None, 784].
    input_layer = features['image']
    #input_layer = tf.reshape(input_layer, shape=[-1, 784])
    logits = mnist.inference(input_layer, hidden1_units=params["hidden1_units"],
                             hidden2_units=params["hidden2_units"])
    serving_key = tf.saved_model.signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY
    # Generate a dictionary of inference output name to tensor (for PREDICT mode)
    # Tensor outputs corresponding to the DEFAULT_SERVING_SIGNATURE_DEF_KEY are produced as output columns of
    # the TFTransformer generated by fitting our estimator
    predictions = {
        "classes": tf.argmax(input=logits, axis=1, name="classes_tensor"),
        "probabilities": tf.nn.softmax(logits, name="softmax_tensor"),
    }
    export_outputs = {serving_key: tf.estimator.export.PredictOutput(predictions)}
    # If the estimator is running in PREDICT mode, you can stop building our model graph here and simply return
    # our model's inference outputs
    if mode == tf.estimator.ModeKeys.PREDICT:
        return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions,
                                          export_outputs=export_outputs)
    # Calculate Loss (for both TRAIN and EVAL modes)
    onehot_labels = tf.one_hot(indices=tf.cast(labels, tf.int32), depth=10)
    loss = tf.losses.softmax_cross_entropy(onehot_labels=onehot_labels, logits=logits)
    if mode == tf.estimator.ModeKeys.TRAIN:
        # Set up logging hooks; these run on every worker.
        logging_hooks = [tf.train.LoggingTensorHook(tensors={"predictions": "classes_tensor"}, every_n_iter=5000)]
        # Horovod: scale learning rate by the number of workers, add distributed optimizer
        optimizer = tf.train.MomentumOptimizer(
            learning_rate=0.001 * hvd.size(), momentum=0.9)
        optimizer = hvd.DistributedOptimizer(optimizer)
        train_op = optimizer.minimize(
            loss=loss,
            global_step=tf.train.get_global_step())
        return tf.estimator.EstimatorSpec(mode=mode, loss=loss, train_op=train_op,
                                          export_outputs=export_outputs,
                                          training_hooks=logging_hooks)
    # If running in EVAL mode, add model evaluation metrics (accuracy) to your EstimatorSpec so that
    # they're logged when model evaluation runs
    eval_metric_ops = {"accuracy": tf.metrics.accuracy(
        labels=labels, predictions=predictions["classes"])}
    return tf.estimator.EstimatorSpec(
        mode=mode, loss=loss, eval_metric_ops=eval_metric_ops, export_outputs=export_outputs)



# COMMAND ----------

# Model checkpoints will be saved to the driver machine's local filesystem.
model_dir = "/tmp/horovod_estimator"
dbutils.fs.rm(model_dir[5:], recurse=True)
# Create estimator
est = HorovodEstimator(modelFn=model_fn,
                       featureMapping={"image": "image"},
                       modelDir=model_dir,
                       labelCol="label_col",
                       batchSize=64,
                       maxSteps=5000,
                       isValidationCol="isVal",
                       modelFnParams={"hidden1_units": 100, "hidden2_units": 50},
                       saveCheckpointsSecs=30)

# COMMAND ----------

# Add column indicating whether each row is in the training/validation set; we perform a random split of the data
df_with_val = df.withColumn("isVal", when(rand() > 0.8, True).otherwise(False))
# Fit estimator to obtain a TFTransformer
transformer = est.fit(df_with_val)
# Apply the TFTransformer to our training data and display the results. Note that our predicted "classes" tend to
# match the label column in our training set.
res = transformer.transform(df)
display(res)

# COMMAND ----------

est.setMaxSteps(10000)
new_transformer = est.fit(df_with_val)
new_res = transformer.transform(df)
display(new_res)

# COMMAND ----------

dbutils.fs.cp("file:/tmp/horovod_estimator/", "dbfs:/horovod_estimator/", recurse=True)

# COMMAND ----------

# MAGIC %sh 
# MAGIC ls -ltr /tmp/horovod_estimator

# COMMAND ----------

print(dbutils.fs.ls("dbfs:/horovod_estimator/"))

# COMMAND ----------

# MAGIC %sh
# MAGIC rm -rf /tmp/horovod_estimator

# COMMAND ----------

# MAGIC %sh 
# MAGIC ls -ltr /tmp/horovod_estimator

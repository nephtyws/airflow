# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import json
import sys
from typing import TYPE_CHECKING, List, Optional, Sequence

from botocore.exceptions import ClientError

from airflow.exceptions import AirflowException
from airflow.models import BaseOperator
from airflow.providers.amazon.aws.hooks.base_aws import AwsBaseHook
from airflow.providers.amazon.aws.hooks.sagemaker import SageMakerHook

if sys.version_info >= (3, 8):
    from functools import cached_property
else:
    from cached_property import cached_property

if TYPE_CHECKING:
    from airflow.utils.context import Context


class SageMakerBaseOperator(BaseOperator):
    """This is the base operator for all SageMaker operators.

    :param config: The configuration necessary to start a training job (templated)
    :type config: dict
    :param aws_conn_id: The AWS connection ID to use.
    :type aws_conn_id: str
    """

    template_fields: Sequence[str] = ('config',)
    template_ext: Sequence[str] = ()
    template_fields_renderers = {'config': 'json'}
    ui_color = '#ededed'
    integer_fields = []

    def __init__(self, *, config: dict, aws_conn_id: str = 'aws_default', **kwargs):
        super().__init__(**kwargs)
        self.aws_conn_id = aws_conn_id
        self.config = config

    def parse_integer(self, config, field):
        """Recursive method for parsing string fields holding integer values to integers."""
        if len(field) == 1:
            if isinstance(config, list):
                for sub_config in config:
                    self.parse_integer(sub_config, field)
                return
            head = field[0]
            if head in config:
                config[head] = int(config[head])
            return
        if isinstance(config, list):
            for sub_config in config:
                self.parse_integer(sub_config, field)
            return
        (head, tail) = (field[0], field[1:])
        if head in config:
            self.parse_integer(config[head], tail)
        return

    def parse_config_integers(self):
        """
        Parse the integer fields of training config to integers in case the config is rendered by Jinja and
        all fields are str
        """
        for field in self.integer_fields:
            self.parse_integer(self.config, field)

    def expand_role(self):
        """Placeholder for calling boto3's `expand_role`, which expands an IAM role name into an ARN."""

    def preprocess_config(self):
        """Process the config into a usable form."""
        self.log.info('Preprocessing the config and doing required s3_operations')
        self.hook.configure_s3_resources(self.config)
        self.parse_config_integers()
        self.expand_role()
        self.log.info(
            'After preprocessing the config is:\n %s',
            json.dumps(self.config, sort_keys=True, indent=4, separators=(',', ': ')),
        )

    def execute(self, context: 'Context'):
        raise NotImplementedError('Please implement execute() in sub class!')

    @cached_property
    def hook(self):
        """Return SageMakerHook"""
        return SageMakerHook(aws_conn_id=self.aws_conn_id)


class SageMakerProcessingOperator(SageMakerBaseOperator):
    """Initiate a SageMaker processing job.

    This operator returns The ARN of the processing job created in Amazon SageMaker.

    :param config: The configuration necessary to start a processing job (templated).

        For details of the configuration parameter see :py:meth:`SageMaker.Client.create_processing_job`
    :type config: dict
    :param aws_conn_id: The AWS connection ID to use.
    :type aws_conn_id: str
    :param wait_for_completion: If wait is set to True, the time interval, in seconds,
        that the operation waits to check the status of the processing job.
    :type wait_for_completion: bool
    :param print_log: if the operator should print the cloudwatch log during processing
    :type print_log: bool
    :param check_interval: if wait is set to be true, this is the time interval
        in seconds which the operator will check the status of the processing job
    :type check_interval: int
    :param max_ingestion_time: If wait is set to True, the operation fails if the processing job
        doesn't finish within max_ingestion_time seconds. If you set this parameter to None,
        the operation does not timeout.
    :type max_ingestion_time: int
    :param action_if_job_exists: Behaviour if the job name already exists. Possible options are "increment"
        (default) and "fail".
    :type action_if_job_exists: str
    """

    def __init__(
        self,
        *,
        config: dict,
        aws_conn_id: str,
        wait_for_completion: bool = True,
        print_log: bool = True,
        check_interval: int = 30,
        max_ingestion_time: Optional[int] = None,
        action_if_job_exists: str = 'increment',
        **kwargs,
    ):
        super().__init__(config=config, aws_conn_id=aws_conn_id, **kwargs)
        if action_if_job_exists not in ('increment', 'fail'):
            raise AirflowException(
                f"Argument action_if_job_exists accepts only 'increment' and 'fail'. \
                Provided value: '{action_if_job_exists}'."
            )
        self.action_if_job_exists = action_if_job_exists
        self.wait_for_completion = wait_for_completion
        self.print_log = print_log
        self.check_interval = check_interval
        self.max_ingestion_time = max_ingestion_time
        self._create_integer_fields()

    def _create_integer_fields(self) -> None:
        """Set fields which should be casted to integers."""
        self.integer_fields = [
            ['ProcessingResources', 'ClusterConfig', 'InstanceCount'],
            ['ProcessingResources', 'ClusterConfig', 'VolumeSizeInGB'],
        ]
        if 'StoppingCondition' in self.config:
            self.integer_fields += [['StoppingCondition', 'MaxRuntimeInSeconds']]

    def expand_role(self) -> None:
        if 'RoleArn' in self.config:
            hook = AwsBaseHook(self.aws_conn_id, client_type='iam')
            self.config['RoleArn'] = hook.expand_role(self.config['RoleArn'])

    def execute(self, context: 'Context') -> dict:
        self.preprocess_config()
        processing_job_name = self.config['ProcessingJobName']
        if self.hook.find_processing_job_by_name(processing_job_name):
            raise AirflowException(
                f'A SageMaker processing job with name {processing_job_name} already exists.'
            )
        self.log.info('Creating SageMaker processing job %s.', self.config['ProcessingJobName'])
        response = self.hook.create_processing_job(
            self.config,
            wait_for_completion=self.wait_for_completion,
            check_interval=self.check_interval,
            max_ingestion_time=self.max_ingestion_time,
        )
        if response['ResponseMetadata']['HTTPStatusCode'] != 200:
            raise AirflowException(f'Sagemaker Processing Job creation failed: {response}')
        return {'Processing': self.hook.describe_processing_job(self.config['ProcessingJobName'])}


class SageMakerEndpointConfigOperator(SageMakerBaseOperator):
    """
    Create a SageMaker endpoint config.

    This operator returns The ARN of the endpoint config created in Amazon SageMaker

    :param config: The configuration necessary to create an endpoint config.

        For details of the configuration parameter see :py:meth:`SageMaker.Client.create_endpoint_config`
    :type config: dict
    :param aws_conn_id: The AWS connection ID to use.
    :type aws_conn_id: str
    """

    integer_fields = [['ProductionVariants', 'InitialInstanceCount']]

    def __init__(self, *, config: dict, **kwargs):
        super().__init__(config=config, **kwargs)
        self.config = config

    def execute(self, context: 'Context') -> dict:
        self.preprocess_config()
        self.log.info('Creating SageMaker Endpoint Config %s.', self.config['EndpointConfigName'])
        response = self.hook.create_endpoint_config(self.config)
        if response['ResponseMetadata']['HTTPStatusCode'] != 200:
            raise AirflowException(f'Sagemaker endpoint config creation failed: {response}')
        else:
            return {'EndpointConfig': self.hook.describe_endpoint_config(self.config['EndpointConfigName'])}


class SageMakerEndpointOperator(SageMakerBaseOperator):
    """
    Create a SageMaker endpoint.

    This operator returns The ARN of the endpoint created in Amazon SageMaker

    :param config:
        The configuration necessary to create an endpoint.

        If you need to create a SageMaker endpoint based on an existed
        SageMaker model and an existed SageMaker endpoint config::

            config = endpoint_configuration;

        If you need to create all of SageMaker model, SageMaker endpoint-config and SageMaker endpoint::

            config = {
                'Model': model_configuration,
                'EndpointConfig': endpoint_config_configuration,
                'Endpoint': endpoint_configuration
            }

        For details of the configuration parameter of model_configuration see
        :py:meth:`SageMaker.Client.create_model`

        For details of the configuration parameter of endpoint_config_configuration see
        :py:meth:`SageMaker.Client.create_endpoint_config`

        For details of the configuration parameter of endpoint_configuration see
        :py:meth:`SageMaker.Client.create_endpoint`

    :type config: dict
    :param aws_conn_id: The AWS connection ID to use.
    :type aws_conn_id: str
    :param wait_for_completion: Whether the operator should wait until the endpoint creation finishes.
    :type wait_for_completion: bool
    :param check_interval: If wait is set to True, this is the time interval, in seconds, that this operation
        waits before polling the status of the endpoint creation.
    :type check_interval: int
    :param max_ingestion_time: If wait is set to True, this operation fails if the endpoint creation doesn't
        finish within max_ingestion_time seconds. If you set this parameter to None it never times out.
    :type max_ingestion_time: int
    :param operation: Whether to create an endpoint or update an endpoint. Must be either 'create or 'update'.
    :type operation: str
    """

    def __init__(
        self,
        *,
        config: dict,
        wait_for_completion: bool = True,
        check_interval: int = 30,
        max_ingestion_time: Optional[int] = None,
        operation: str = 'create',
        **kwargs,
    ):
        super().__init__(config=config, **kwargs)
        self.config = config
        self.wait_for_completion = wait_for_completion
        self.check_interval = check_interval
        self.max_ingestion_time = max_ingestion_time
        self.operation = operation.lower()
        if self.operation not in ['create', 'update']:
            raise ValueError('Invalid value! Argument operation has to be one of "create" and "update"')
        self.create_integer_fields()

    def create_integer_fields(self) -> None:
        """Set fields which should be casted to integers."""
        if 'EndpointConfig' in self.config:
            self.integer_fields = [['EndpointConfig', 'ProductionVariants', 'InitialInstanceCount']]

    def expand_role(self) -> None:
        if 'Model' not in self.config:
            return
        hook = AwsBaseHook(self.aws_conn_id, client_type='iam')
        config = self.config['Model']
        if 'ExecutionRoleArn' in config:
            config['ExecutionRoleArn'] = hook.expand_role(config['ExecutionRoleArn'])

    def execute(self, context: 'Context') -> dict:
        self.preprocess_config()
        model_info = self.config.get('Model')
        endpoint_config_info = self.config.get('EndpointConfig')
        endpoint_info = self.config.get('Endpoint', self.config)
        if model_info:
            self.log.info('Creating SageMaker model %s.', model_info['ModelName'])
            self.hook.create_model(model_info)
        if endpoint_config_info:
            self.log.info('Creating endpoint config %s.', endpoint_config_info['EndpointConfigName'])
            self.hook.create_endpoint_config(endpoint_config_info)
        if self.operation == 'create':
            sagemaker_operation = self.hook.create_endpoint
            log_str = 'Creating'
        elif self.operation == 'update':
            sagemaker_operation = self.hook.update_endpoint
            log_str = 'Updating'
        else:
            raise ValueError('Invalid value! Argument operation has to be one of "create" and "update"')
        self.log.info('%s SageMaker endpoint %s.', log_str, endpoint_info['EndpointName'])
        try:
            response = sagemaker_operation(
                endpoint_info,
                wait_for_completion=self.wait_for_completion,
                check_interval=self.check_interval,
                max_ingestion_time=self.max_ingestion_time,
            )
        except ClientError:
            self.operation = 'update'
            sagemaker_operation = self.hook.update_endpoint
            log_str = 'Updating'
            response = sagemaker_operation(
                endpoint_info,
                wait_for_completion=self.wait_for_completion,
                check_interval=self.check_interval,
                max_ingestion_time=self.max_ingestion_time,
            )
        if response['ResponseMetadata']['HTTPStatusCode'] != 200:
            raise AirflowException(f'Sagemaker endpoint creation failed: {response}')
        else:
            return {
                'EndpointConfig': self.hook.describe_endpoint_config(endpoint_info['EndpointConfigName']),
                'Endpoint': self.hook.describe_endpoint(endpoint_info['EndpointName']),
            }


class SageMakerTransformOperator(SageMakerBaseOperator):
    """Initiate a SageMaker transform job.

    This operator returns The ARN of the model created in Amazon SageMaker.

    :param config: The configuration necessary to start a transform job (templated).

        If you need to create a SageMaker transform job based on an existed SageMaker model::

            config = transform_config

        If you need to create both SageMaker model and SageMaker Transform job::

            config = {
                'Model': model_config,
                'Transform': transform_config
            }

        For details of the configuration parameter of transform_config see
        :py:meth:`SageMaker.Client.create_transform_job`

        For details of the configuration parameter of model_config, See:
        :py:meth:`SageMaker.Client.create_model`

    :type config: dict
    :param aws_conn_id: The AWS connection ID to use.
    :type aws_conn_id: str
    :param wait_for_completion: Set to True to wait until the transform job finishes.
    :type wait_for_completion: bool
    :param check_interval: If wait is set to True, the time interval, in seconds,
        that this operation waits to check the status of the transform job.
    :type check_interval: int
    :param max_ingestion_time: If wait is set to True, the operation fails
        if the transform job doesn't finish within max_ingestion_time seconds. If you
        set this parameter to None, the operation does not timeout.
    :type max_ingestion_time: int
    """

    def __init__(
        self,
        *,
        config: dict,
        wait_for_completion: bool = True,
        check_interval: int = 30,
        max_ingestion_time: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(config=config, **kwargs)
        self.config = config
        self.wait_for_completion = wait_for_completion
        self.check_interval = check_interval
        self.max_ingestion_time = max_ingestion_time
        self.create_integer_fields()

    def create_integer_fields(self) -> None:
        """Set fields which should be casted to integers."""
        self.integer_fields: List[List[str]] = [
            ['Transform', 'TransformResources', 'InstanceCount'],
            ['Transform', 'MaxConcurrentTransforms'],
            ['Transform', 'MaxPayloadInMB'],
        ]
        if 'Transform' not in self.config:
            for field in self.integer_fields:
                field.pop(0)

    def expand_role(self) -> None:
        if 'Model' not in self.config:
            return
        config = self.config['Model']
        if 'ExecutionRoleArn' in config:
            hook = AwsBaseHook(self.aws_conn_id, client_type='iam')
            config['ExecutionRoleArn'] = hook.expand_role(config['ExecutionRoleArn'])

    def execute(self, context: 'Context') -> dict:
        self.preprocess_config()
        model_config = self.config.get('Model')
        transform_config = self.config.get('Transform', self.config)
        if model_config:
            self.log.info('Creating SageMaker Model %s for transform job', model_config['ModelName'])
            self.hook.create_model(model_config)
        self.log.info('Creating SageMaker transform Job %s.', transform_config['TransformJobName'])
        response = self.hook.create_transform_job(
            transform_config,
            wait_for_completion=self.wait_for_completion,
            check_interval=self.check_interval,
            max_ingestion_time=self.max_ingestion_time,
        )
        if response['ResponseMetadata']['HTTPStatusCode'] != 200:
            raise AirflowException(f'Sagemaker transform Job creation failed: {response}')
        else:
            return {
                'Model': self.hook.describe_model(transform_config['ModelName']),
                'Transform': self.hook.describe_transform_job(transform_config['TransformJobName']),
            }


class SageMakerTuningOperator(SageMakerBaseOperator):
    """Initiate a SageMaker hyperparameter tuning job.

    This operator returns The ARN of the tuning job created in Amazon SageMaker.

    :param config: The configuration necessary to start a tuning job (templated).

        For details of the configuration parameter see
        :py:meth:`SageMaker.Client.create_hyper_parameter_tuning_job`
    :type config: dict
    :param aws_conn_id: The AWS connection ID to use.
    :type aws_conn_id: str
    :param wait_for_completion: Set to True to wait until the tuning job finishes.
    :type wait_for_completion: bool
    :param check_interval: If wait is set to True, the time interval, in seconds,
        that this operation waits to check the status of the tuning job.
    :type check_interval: int
    :param max_ingestion_time: If wait is set to True, the operation fails
        if the tuning job doesn't finish within max_ingestion_time seconds. If you
        set this parameter to None, the operation does not timeout.
    :type max_ingestion_time: int
    """

    integer_fields = [
        ['HyperParameterTuningJobConfig', 'ResourceLimits', 'MaxNumberOfTrainingJobs'],
        ['HyperParameterTuningJobConfig', 'ResourceLimits', 'MaxParallelTrainingJobs'],
        ['TrainingJobDefinition', 'ResourceConfig', 'InstanceCount'],
        ['TrainingJobDefinition', 'ResourceConfig', 'VolumeSizeInGB'],
        ['TrainingJobDefinition', 'StoppingCondition', 'MaxRuntimeInSeconds'],
    ]

    def __init__(
        self,
        *,
        config: dict,
        wait_for_completion: bool = True,
        check_interval: int = 30,
        max_ingestion_time: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(config=config, **kwargs)
        self.config = config
        self.wait_for_completion = wait_for_completion
        self.check_interval = check_interval
        self.max_ingestion_time = max_ingestion_time

    def expand_role(self) -> None:
        if 'TrainingJobDefinition' in self.config:
            config = self.config['TrainingJobDefinition']
            if 'RoleArn' in config:
                hook = AwsBaseHook(self.aws_conn_id, client_type='iam')
                config['RoleArn'] = hook.expand_role(config['RoleArn'])

    def execute(self, context: 'Context') -> dict:
        self.preprocess_config()
        self.log.info(
            'Creating SageMaker Hyper-Parameter Tuning Job %s', self.config['HyperParameterTuningJobName']
        )
        response = self.hook.create_tuning_job(
            self.config,
            wait_for_completion=self.wait_for_completion,
            check_interval=self.check_interval,
            max_ingestion_time=self.max_ingestion_time,
        )
        if response['ResponseMetadata']['HTTPStatusCode'] != 200:
            raise AirflowException(f'Sagemaker Tuning Job creation failed: {response}')
        else:
            return {'Tuning': self.hook.describe_tuning_job(self.config['HyperParameterTuningJobName'])}


class SageMakerModelOperator(SageMakerBaseOperator):
    """Create a SageMaker model.

    This operator returns The ARN of the model created in Amazon SageMaker

    :param config: The configuration necessary to create a model.

        For details of the configuration parameter see :py:meth:`SageMaker.Client.create_model`
    :type config: dict
    :param aws_conn_id: The AWS connection ID to use.
    :type aws_conn_id: str
    """

    def __init__(self, *, config, **kwargs):
        super().__init__(config=config, **kwargs)
        self.config = config

    def expand_role(self) -> None:
        if 'ExecutionRoleArn' in self.config:
            hook = AwsBaseHook(self.aws_conn_id, client_type='iam')
            self.config['ExecutionRoleArn'] = hook.expand_role(self.config['ExecutionRoleArn'])

    def execute(self, context: 'Context') -> dict:
        self.preprocess_config()
        self.log.info('Creating SageMaker Model %s.', self.config['ModelName'])
        response = self.hook.create_model(self.config)
        if response['ResponseMetadata']['HTTPStatusCode'] != 200:
            raise AirflowException(f'Sagemaker model creation failed: {response}')
        else:
            return {'Model': self.hook.describe_model(self.config['ModelName'])}


class SageMakerTrainingOperator(SageMakerBaseOperator):
    """
    Initiate a SageMaker training job.

    This operator returns The ARN of the training job created in Amazon SageMaker.

    :param config: The configuration necessary to start a training job (templated).

        For details of the configuration parameter see :py:meth:`SageMaker.Client.create_training_job`
    :type config: dict
    :param aws_conn_id: The AWS connection ID to use.
    :type aws_conn_id: str
    :param wait_for_completion: If wait is set to True, the time interval, in seconds,
        that the operation waits to check the status of the training job.
    :type wait_for_completion: bool
    :param print_log: if the operator should print the cloudwatch log during training
    :type print_log: bool
    :param check_interval: if wait is set to be true, this is the time interval
        in seconds which the operator will check the status of the training job
    :type check_interval: int
    :param max_ingestion_time: If wait is set to True, the operation fails if the training job
        doesn't finish within max_ingestion_time seconds. If you set this parameter to None,
        the operation does not timeout.
    :type max_ingestion_time: int
    :param check_if_job_exists: If set to true, then the operator will check whether a training job
        already exists for the name in the config.
    :type check_if_job_exists: bool
    :param action_if_job_exists: Behaviour if the job name already exists. Possible options are "increment"
        (default) and "fail".
        This is only relevant if check_if
    """

    integer_fields = [
        ['ResourceConfig', 'InstanceCount'],
        ['ResourceConfig', 'VolumeSizeInGB'],
        ['StoppingCondition', 'MaxRuntimeInSeconds'],
    ]

    def __init__(
        self,
        *,
        config: dict,
        wait_for_completion: bool = True,
        print_log: bool = True,
        check_interval: int = 30,
        max_ingestion_time: Optional[int] = None,
        check_if_job_exists: bool = True,
        action_if_job_exists: str = 'increment',
        **kwargs,
    ):
        super().__init__(config=config, **kwargs)
        self.wait_for_completion = wait_for_completion
        self.print_log = print_log
        self.check_interval = check_interval
        self.max_ingestion_time = max_ingestion_time
        self.check_if_job_exists = check_if_job_exists
        if action_if_job_exists in ('increment', 'fail'):
            self.action_if_job_exists = action_if_job_exists
        else:
            raise AirflowException(
                f"Argument action_if_job_exists accepts only 'increment' and 'fail'. \
                Provided value: '{action_if_job_exists}'."
            )

    def expand_role(self) -> None:
        if 'RoleArn' in self.config:
            hook = AwsBaseHook(self.aws_conn_id, client_type='iam')
            self.config['RoleArn'] = hook.expand_role(self.config['RoleArn'])

    def execute(self, context: 'Context') -> dict:
        self.preprocess_config()
        if self.check_if_job_exists:
            self._check_if_job_exists()
        self.log.info('Creating SageMaker training job %s.', self.config['TrainingJobName'])
        response = self.hook.create_training_job(
            self.config,
            wait_for_completion=self.wait_for_completion,
            print_log=self.print_log,
            check_interval=self.check_interval,
            max_ingestion_time=self.max_ingestion_time,
        )
        if response['ResponseMetadata']['HTTPStatusCode'] != 200:
            raise AirflowException(f'Sagemaker Training Job creation failed: {response}')
        else:
            return {'Training': self.hook.describe_training_job(self.config['TrainingJobName'])}

    def _check_if_job_exists(self) -> None:
        training_job_name = self.config['TrainingJobName']
        training_jobs = self.hook.list_training_jobs(name_contains=training_job_name)
        if training_job_name in [tj['TrainingJobName'] for tj in training_jobs]:
            if self.action_if_job_exists == 'increment':
                self.log.info("Found existing training job with name '%s'.", training_job_name)
                new_training_job_name = f'{training_job_name}-{(len(training_jobs) + 1)}'
                self.config['TrainingJobName'] = new_training_job_name
                self.log.info("Incremented training job name to '%s'.", new_training_job_name)
            elif self.action_if_job_exists == 'fail':
                raise AirflowException(
                    f'A SageMaker training job with name {training_job_name} already exists.'
                )

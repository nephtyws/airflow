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

from typing import Callable, Optional, Sequence, TypeVar

from airflow.decorators.base import DecoratedOperator, task_decorator_factory
from airflow.operators.python import PythonOperator


class _PythonDecoratedOperator(DecoratedOperator, PythonOperator):
    """
    Wraps a Python callable and captures args/kwargs when called for execution.

    :param python_callable: A reference to an object that is callable
    :type python_callable: python callable
    :param op_kwargs: a dictionary of keyword arguments that will get unpacked
        in your function (templated)
    :type op_kwargs: dict
    :param op_args: a list of positional arguments that will get unpacked when
        calling your callable (templated)
    :type op_args: list
    :param multiple_outputs: if set, function return value will be
        unrolled to multiple XCom values. Dict will unroll to xcom values with keys as keys.
        Defaults to False.
    :type multiple_outputs: bool
    """

    template_fields: Sequence[str] = ('op_args', 'op_kwargs')
    template_fields_renderers = {"op_args": "py", "op_kwargs": "py"}

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects (e.g protobuf).
    shallow_copy_attrs: Sequence[str] = ('python_callable',)

    def __init__(
        self,
        **kwargs,
    ) -> None:
        kwargs_to_upstream = {
            "python_callable": kwargs["python_callable"],
            "op_args": kwargs["op_args"],
            "op_kwargs": kwargs["op_kwargs"],
        }

        super().__init__(kwargs_to_upstream=kwargs_to_upstream, **kwargs)


T = TypeVar("T", bound=Callable)


class PythonDecoratorMixin:
    """
    Helper class for inheritance. This class is only used for the __init__.pyi so that IDEs
    will autocomplete docker decorator functions

    :meta private:
    """

    def __call__(
        self, python_callable: Optional[Callable] = None, multiple_outputs: Optional[bool] = None, **kwargs
    ):
        return self.python(python_callable, multiple_outputs, **kwargs)

    def python(
        self, python_callable: Optional[Callable] = None, multiple_outputs: Optional[bool] = None, **kwargs
    ):
        """
        Python operator decorator. Wraps a function into an Airflow operator.

        Accepts kwargs for operator kwarg. Can be reused in a single DAG.

        :param python_callable: Function to decorate
        :type python_callable: Optional[Callable]
        :param multiple_outputs: if set, function return value will be
            unrolled to multiple XCom values. List/Tuples will unroll to xcom values
            with index as key. Dict will unroll to xcom values with keys as XCom keys.
            Defaults to False.
        :type multiple_outputs: bool
        """
        return task_decorator_factory(
            python_callable=python_callable,
            multiple_outputs=multiple_outputs,
            decorated_operator_class=_PythonDecoratedOperator,
            **kwargs,
        )


def python_task(
    python_callable: Optional[Callable] = None, multiple_outputs: Optional[bool] = None, **kwargs
):
    """
    Wraps a function into an Airflow operator.

    Accepts kwargs for operator kwarg. Can be reused in a single DAG.

    :param python_callable: Function to decorate
    :type python_callable: Optional[Callable]
    :param multiple_outputs: if set, function return value will be
        unrolled to multiple XCom values. List/Tuples will unroll to xcom values
        with index as key. Dict will unroll to xcom values with keys as XCom keys.
        Defaults to False.
    :type multiple_outputs: bool
    """
    return task_decorator_factory(
        python_callable=python_callable,
        multiple_outputs=multiple_outputs,
        decorated_operator_class=_PythonDecoratedOperator,
        **kwargs,
    )

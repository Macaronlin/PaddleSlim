# Copyright (c) 2023  PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
sys.path.append("../../")
import os
import unittest
import paddle
import tempfile
import numpy as np

from paddle.vision.models import resnet18
from paddle.quantization import QuantConfig
from paddle.quantization import PTQ

from paddleslim.quant.observers import HistObserver, KLObserver, EMDObserver, MSEObserver, AVGObserver
from paddleslim.quant.observers.hist import PercentHistObserverLayer
from paddleslim.quant.observers.kl import KLObserverLayer
from paddleslim.quant.observers.mse import MSEObserverLayer
from paddleslim.quant.observers.avg import AVGObserverLayer
from paddleslim.quant.observers.emd import EMDObserverLayer
from paddleslim.quant.observers.kl import KLObserverLayer
from paddle.nn.quant.format import LinearDequanter, LinearQuanter

import logging
from paddleslim.common import get_logger
_logger = get_logger(__name__, level=logging.INFO)


class ImperativeLenet(paddle.nn.Layer):
    def __init__(self, num_classes=10, classifier_activation='softmax'):
        super(ImperativeLenet, self).__init__()
        self.features = paddle.nn.Sequential(
            paddle.nn.Conv2D(
                in_channels=1,
                out_channels=6,
                kernel_size=3,
                stride=1,
                padding=1),
            paddle.nn.AvgPool2D(kernel_size=2, stride=2),
            paddle.nn.Conv2D(
                in_channels=6,
                out_channels=16,
                kernel_size=5,
                stride=1,
                padding=0), paddle.nn.AvgPool2D(kernel_size=2, stride=2))

        self.fc = paddle.nn.Sequential(
            paddle.nn.Linear(in_features=400, out_features=120),
            paddle.nn.Linear(in_features=120, out_features=84),
            paddle.nn.Linear(in_features=84, out_features=num_classes), )

    def forward(self, inputs):
        x = self.features(inputs)

        x = paddle.flatten(x, 1)
        x = self.fc(x)
        return x


class TestPTQObserverAcc(unittest.TestCase):
    def __init__(self, observer, observer_type, *args, **kvargs):
        super(TestPTQObserverAcc, self).__init__(*args, **kvargs)
        self.observer = observer
        self.observer_type = observer_type

    def setUp(self):
        paddle.set_device("cpu")
        self.init_case()
        self.dummy_input = paddle.rand([1, 3, 224, 224])
        self.temp_dir = tempfile.TemporaryDirectory(dir="./")
        self.path = os.path.join(self.temp_dir.name, 'qat')
        if not os.path.exists('ILSVRC2012_data_demo'):
            os.system(
                'wget -q https://sys-p0.bj.bcebos.com/slim_ci/ILSVRC2012_data_demo.tar.gz'
            )
            os.system('tar -xf ILSVRC2012_data_demo.tar.gz')
        seed = 1
        np.random.seed(seed)
        paddle.static.default_main_program().random_seed = seed
        paddle.static.default_startup_program().random_seed = seed

    def tearDown(self):
        self.temp_dir.cleanup()

    def runTest(self):
        self.test_convergence()

    def init_case(self):
        self.q_config = QuantConfig(activation=None, weight=None)
        self.q_config.add_type_config(
            paddle.nn.Conv2D, activation=self.observer, weight=self.observer)

    def _count_layers(self, model, layer_type):
        count = 0
        for _layer in model.sublayers(True):
            if isinstance(_layer, layer_type):
                count += 1
        return count

    def test_convergence(self):
        model = ImperativeLenet()
        place = paddle.CUDAPlace(0) \
            if paddle.is_compiled_with_cuda() else paddle.CPUPlace()

        transform = paddle.vision.transforms.Compose([
            paddle.vision.transforms.Transpose(),
            paddle.vision.transforms.Normalize([127.5], [127.5])
        ])

        train_dataset = paddle.vision.datasets.MNIST(
            mode='train', backend='cv2', transform=transform)
        val_dataset = paddle.vision.datasets.MNIST(
            mode='test', backend='cv2', transform=transform)

        train_reader = paddle.io.DataLoader(
            train_dataset,
            drop_last=True,
            places=place,
            batch_size=64,
            return_list=True)
        test_reader = paddle.io.DataLoader(
            val_dataset, places=place, batch_size=64, return_list=True)

        def train(model):
            adam = paddle.optimizer.Adam(
                learning_rate=0.0001, parameters=model.parameters())
            epoch_num = 1
            for epoch in range(epoch_num):
                model.train()
                for batch_id, data in enumerate(train_reader):
                    img = paddle.to_tensor(data[0])
                    label = paddle.to_tensor(data[1])
                    img = paddle.reshape(img, [-1, 1, 28, 28])
                    label = paddle.reshape(label, [-1, 1])

                    out = model(img)
                    acc = paddle.metric.accuracy(out, label)
                    loss = paddle.nn.functional.loss.cross_entropy(out, label)
                    avg_loss = paddle.mean(loss)
                    avg_loss.backward()
                    adam.minimize(avg_loss)
                    model.clear_gradients()
                    if batch_id % 100 == 0:
                        _logger.info(
                            "Train | At epoch {} step {}: loss = {:}, acc= {:}".
                            format(epoch, batch_id,
                                   avg_loss.numpy(), acc.numpy()))

        def test(model):
            model.eval()
            avg_acc = [[], []]
            for batch_id, data in enumerate(test_reader):
                img = paddle.to_tensor(data[0])
                img = paddle.reshape(img, [-1, 1, 28, 28])
                label = paddle.to_tensor(data[1])
                label = paddle.reshape(label, [-1, 1])

                out = model(img)
                acc_top1 = paddle.metric.accuracy(input=out, label=label, k=1)
                acc_top5 = paddle.metric.accuracy(input=out, label=label, k=5)
                avg_acc[0].append(acc_top1.numpy())
                avg_acc[1].append(acc_top5.numpy())
                if batch_id % 100 == 0:
                    _logger.info(
                        "Test | step {}: acc1 = {:}, acc5 = {:}".format(
                            batch_id, acc_top1.numpy(), acc_top5.numpy()))

            _logger.info("Test | Average: acc_top1 {}, acc_top5 {}".format(
                np.mean(avg_acc[0]), np.mean(avg_acc[1])))
            return np.mean(avg_acc[0]), np.mean(avg_acc[1])

        def ptq_sample(model):
            model.eval()
            avg_acc = [[], []]
            for batch_id, data in enumerate(test_reader):
                img = paddle.to_tensor(data[0])
                img = paddle.reshape(img, [-1, 1, 28, 28])
                label = paddle.to_tensor(data[1])
                label = paddle.reshape(label, [-1, 1])

                out = model(img)

                if batch_id % 100 == 0:
                    _logger.info("PTQ sampling | step {}".format(batch_id))

        train(model)
        top1_1, top5_1 = test(model)
        ptq = PTQ(self.q_config)
        model.eval()
        quant_model = ptq.quantize(model, inplace=False)

        ptq_sample(quant_model)
        converted_model = ptq.convert(quant_model, inplace=False)
        top1_2, top5_2 = test(converted_model)

        _logger.info(
            "Before quantization: top1: {}, top5: {}".format(top1_1, top5_1))
        _logger.info(
            "After quantization: top1: {}, top5: {}".format(top1_2, top5_2))
        _logger.info("\n")

        diff = 0.01
        self.assertTrue(
            top1_1 - top1_2 < diff,
            msg="The acc of quant model is too lower than fp32 model")
        _logger.info('done')
        return


observer_suite = unittest.TestSuite()
observer_suite.addTest(
    TestPTQObserverAcc(
        observer=HistObserver(sign=True, symmetric=True),
        observer_type=PercentHistObserverLayer))
observer_suite.addTest(
    TestPTQObserverAcc(
        observer=KLObserver(bins_count=256), observer_type=KLObserverLayer))

observer_suite.addTest(
    TestPTQObserverAcc(observer=AVGObserver(), observer_type=AVGObserverLayer))
observer_suite.addTest(
    TestPTQObserverAcc(observer=EMDObserver(), observer_type=EMDObserverLayer))
observer_suite.addTest(
    TestPTQObserverAcc(observer=MSEObserver(), observer_type=MSEObserverLayer))

if __name__ == '__main__':
    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(observer_suite)
    os.system('rm -rf ILSVRC2012_data_demo.tar.gz')
    os.system('rm -rf ILSVRC2012_data_demo')

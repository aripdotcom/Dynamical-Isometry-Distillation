import numpy as np
from tqdm import tqdm

import torch
import torch.nn as nn

from ldl.data.utils import get_augmented_images
from ldl.data import get_episodic_loader
from ldl.models import RNDModel


def run_epoch(rnd, loss_func, train_loader, epoch=0, silent=False, device=None):
    for batch_i, (x, y) in enumerate(train_loader):
        x = x.squeeze().to(device)
        y = y.to(device)

        # Activate predictor for the needed class
        rnd.activate_predictor(class_=y.item())

        predictor_feature, target_feature = rnd(x)
        loss = loss_func(predictor_feature, target_feature).mean()
        optimizer = rnd.get_optimizer(y.item())
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if batch_i % 100 == 0 and not silent:
            msg = 'Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'
            print(msg.format(epoch+1, batch_i, len(train_loader),
                         batch_i/len(train_loader)*100, loss.item()))


def test(rnd, test_loader, silent=False, device=None):
    rnd.eval()
    correct = 0
    with torch.no_grad():
        for batch_i, (x, y) in enumerate(test_loader):
            x = x.squeeze()
            predict_next_state_feature, target_next_state_feature = rnd.predict(x.to(device))
            mses = []
            for predict in predict_next_state_feature:
                mses.append((target_next_state_feature - predict).pow(2).sum(0) / 2)
            class_min_mse = np.argmin(mses)
            if class_min_mse == y.item():
                correct += 1
        acc = correct / (batch_i+1)
        if not silent:
            print('Accuracy: {}/{} ({:.0f}%)\n'.format(correct, batch_i+1, 100. * acc))
    return acc


def augment_data(support, way, train_shot):
    """
    Augment data by elementary methods.

    Args:
        support (np.ndarray): data of shape [n_way, n_shot, channels, width, hight]

    Returns (np.ndarray): data of shape [n_augmented, width, height]

    """
    w, h = support.shape[-1], support.shape[-1]
    x_train = support.squeeze().reshape((-1, w, h))
    y_train = [i // train_shot for i in range(train_shot * way)]

    # This should be done in preprocessing step
    imgs_aug = []
    y_aug = []
    for i_img in range(x_train.shape[0]):
        img = x_train[i_img].detach().numpy()

        augmented = get_augmented_images(img, shift=4, sigma=0.03)
        imgs_aug += augmented
        y_aug += [y_train[i_img]] * len(augmented)

    x_aug = np.array(imgs_aug, np.float32)
    y_aug = np.array(y_aug)
    return x_aug, y_aug


def run_experiment(config):
    dataset = config['dataset']
    way = config['way']
    train_shot = config['train_shot']
    test_shot = config['test_shot']
    mse_loss = config['loss']
    trials = config['trials']
    epochs = config['epochs']
    pretrain_epochs = config['pretrain_epochs']
    silent = config['silent']
    split = config['split']
    add_rotations = config['add_rotations']
    in_alphabet = config['in_alphabet']
    x_dim = config['x_dim']
    z_dim = config['z_dim']
    optimizer = config['optimizer']
    lr = config['lr']
    initialization = config['initialization']
    gpu = config['gpu']

    device = torch.device(f"cuda:{gpu}" if torch.cuda.is_available() else "cpu")

    accs = []
    for _ in tqdm(range(trials)):

        data = get_episodic_loader(dataset=dataset,
                                   way=way,
                                   train_shot=train_shot,
                                   test_shot=test_shot,
                                   split=split,
                                   add_rotations=add_rotations,
                                   in_alphabet=in_alphabet, x_dim=x_dim)

        model = RNDModel(way, in_dim=x_dim**2, out_dim=z_dim, opt=optimizer,
                         lr=lr, initialization=initialization)
        model.to(device)

        for sample in data:
            support = sample['xs']
            query = sample['xq']

            # (1) Pretraining on pair samples
            if pretrain_epochs:
                x_train = support.reshape((-1, x_dim**2))
                y_train = [i // train_shot for i in range(train_shot * way)]
                x_pair_samples = []
                y_pair_samples = []
                # for each sample in train find random sample in the same train
                # set and average them. final label is randomly chosen between
                # original label and random chosen.
                for i in tqdm(range(len(x_train)), desc="Pairing samples"):
                    for _ in range(len(x_train)):
                        img_true = x_train[i]
                        i_random = np.random.randint(len(x_train))
                        img_random = x_train[i_random]
                        img_paired = (img_true + img_random) / 2

                        x_pair_samples.append(img_paired.numpy())
                        y_pair_samples.append(y_train[np.random.choice([i, i_random])])
                x_pair_samples = torch.tensor(np.asarray(x_pair_samples))
                y_pair_samples = torch.tensor(np.asarray(y_pair_samples))

            for _ in tqdm(range(pretrain_epochs), desc="Pretraining"):
                inds = np.random.permutation(x_pair_samples.shape[0])
                samples_pretrain = list(zip(x_pair_samples[inds], y_pair_samples[inds]))
                run_epoch(model, loss_func=mse_loss, train_loader=samples_pretrain,
                          silent=silent, device=device)

            # (2) Fine tuning on real samples

            x_train, y_train = augment_data(support, way, train_shot)
            x_train = x_train.reshape((-1, x_dim**2))

            x_test = query.reshape((-1, x_dim**2))
            y_test = np.asarray(
                [i // test_shot for i in range(test_shot * way)])

            x_train = torch.tensor(x_train)
            y_train = torch.tensor(y_train)
            x_test = torch.tensor(x_test)
            y_test = torch.tensor(y_test)

            #print("Train: ", x_train.shape, y_train.shape)
            #print("Test: ", x_test.shape, y_test.shape)

            inds = np.random.permutation(x_train.shape[0])
            samples_train = list(zip(x_train[inds], y_train[inds]))
            samples_test = list(zip(x_test, y_test))

            for i_epoch in range(epochs):
                np.random.shuffle(samples_train)
                run_epoch(model, loss_func=mse_loss, train_loader=samples_train,
                      silent=silent, device=device)
            accs.append(test(model, samples_test, silent=silent, device=device))

    return np.mean(accs)


if __name__ == "__main__":
    np.random.seed(2019)
    torch.manual_seed(2019)

    config = {
        'dataset': 'omniglot',
        'way': 5,
        'train_shot': 5,
        'test_shot': 1,
        'loss': nn.MSELoss(reduction='none'),
        'epochs': 3,
        'pretrain_epochs': 30,
        'trials': 100,
        'silent': True,
        'split': 'test',
        'in_alphabet': False,
        'add_rotations': True,
        'x_dim': 28,
        'z_dim': 500,
        'initialization': 'xavier_normal',
        'optimizer': 'adam',
        'lr': 0.001,
        'channels': 1,
        'gpu': 0
    }
    mean_accuracy = run_experiment(config)
    print("Mean accuracy: ", mean_accuracy)

    # 0.755 accuracy with simple augmentation
    # 0.77 on 3 epochs
    # Got 0.648 with 10 pretrained epochs...
    # Got 0.652 with 30 pretrained epochs...

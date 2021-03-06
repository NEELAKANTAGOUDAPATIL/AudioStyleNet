import argparse
import glob
import os
import numpy as np
import torch
import torch.nn.functional as F

from tqdm import tqdm
from lpips import PerceptualLoss
from my_models import style_gan_2
from PIL import Image
from torchvision import transforms
from torchvision.utils import save_image
from utils import utils


class Projector:
    def __init__(self,
                 g,
                 num_steps=1000,
                 mse_strength=0.,
                 initial_learning_rate=0.1,
                 initial_noise_factor=0.05,
                 lr_rampdown_length=0.25,
                 lr_rampup_length=0.05,
                 noise_ramp_length=0.75,
                 verbose=True,
                 initial_latent=None,
                 ):

        self.num_steps = num_steps
        self.n_mean_latent = 10000
        self.mse_strength = mse_strength
        self.initial_lr = initial_learning_rate
        self.lr = initial_learning_rate
        self.initial_noise_factor = initial_noise_factor
        self.lr_rampdown_length = lr_rampdown_length
        self.lr_rampup_length = lr_rampup_length
        self.noise_ramp_length = noise_ramp_length
        self.regularize_noise_weight = 1e5
        self.verbose = verbose

        self.latent_expr = None
        self.lpips = None
        self.target_images = None
        self.loss = None
        self.cur_step = None

        self.g_ema = g
        self.device = next(g.parameters()).device

        # Find latent stats
        self._info(
            ('Finding W midpoint and stddev using %d samples...' % self.n_mean_latent))
        torch.manual_seed(123)
        with torch.no_grad():
            noise_sample = torch.randn(
                self.n_mean_latent, 512, device=self.device)
            latent_out = self.g_ema.style(noise_sample)

        self.latent_mean = latent_out.mean(0)
        self.latent_std = (
            (latent_out - self.latent_mean).pow(2).sum() / self.n_mean_latent) ** 0.5
        self._info('std = {}'.format(self.latent_std))

        if initial_latent is None:
            self.latent_in = self.latent_mean.detach().clone().unsqueeze(0)
            self.latent_in = self.latent_in.repeat(self.g_ema.n_latent, 1)
        else:
            self.latent_in = initial_latent
        self.latent_in.requires_grad = True

        # Find noise inputs.

        # Init optimizer
        # self.opt = torch.optim.Adam(
        #     [self.latent_in] + self.noises, lr=self.initial_lr)
        self.opt = torch.optim.Adam([self.latent_in], lr=self.initial_lr)

        # Init loss function
        self.lpips = PerceptualLoss(model='net-lin', net='vgg').to(self.device)

    def _info(self, *args):
        if self.verbose:
            print('Projector:', *args)

    def update_lr(self, t):
        lr_ramp = min(1.0, (1.0 - t) / self.lr_rampdown_length)
        lr_ramp = 0.5 - 0.5 * np.cos(lr_ramp * np.pi)
        lr_ramp = lr_ramp * min(1.0, t / self.lr_rampup_length)
        self.lr = self.initial_lr * lr_ramp
        self.opt.param_groups[0]['lr'] = self.lr

    def noise_regularization(self):
        reg_loss = 0.0
        for noise in self.noises:
            size = noise.shape[2]
            while True:
                reg_loss += (noise * noise.roll(1, dims=3)).mean().pow(2) + \
                    (noise * noise.roll(1, dims=2)).mean().pow(2)
                if size <= 8:
                    break  # Small enough already
                noise = noise.reshape(
                    [1, 1, size // 2, 2, size // 2, 2])  # Downscale
                noise = noise.mean(dim=[3, 5])
                size = size // 2
        return reg_loss

    def normalize_noise(self):
        for noise in self.noises:
            mean = noise.mean()
            std = noise.std()
            noise.data.add_(-mean).div_(std)

    def prepare_input(self, target_image):
        if len(target_image.shape) == 3:
            target_image = target_image.unsqueeze(0)
        if target_image.shape[2] > 256:
            target_image = utils.downsample_256(target_image)
        self.target_image = target_image
        print(self.target_image.shape)

    def run(self, target_images, num_steps):
        self.num_steps = num_steps
        self.prepare_input(target_images)

        self._info('Running...')
        pbar = tqdm(range(self.num_steps))
        for i_step in pbar:
            self.cur_step = i_step
            self.step()
            pbar.set_description(
                (f'loss: {self.loss.item():.4f}; lr: {self.lr:.4f}'))

    def step(self):
        # Hyperparameters
        t = self.cur_step / self.num_steps

        # Add noise to dlatents
        noise_strength = self.latent_std * self.initial_noise_factor * \
            max(0.0, 1.0 - t / self.noise_ramp_length) ** 2
        latent_noise = (torch.randn_like(self.latent_in)
                        * noise_strength).to(self.device)
        self.latent_expr = self.latent_in + latent_noise

        # Update learning rate
        self.update_lr(t)

        # Train
        self.img_gen = self.g_ema(
            [self.latent_expr.unsqueeze(0)], input_is_latent=True, noise=self.g_ema.noises)[0]

        # Downsample to 256 x 256
        self.img_gen = utils.downsample_256(self.img_gen)

        # Compute perceptual loss
        self.loss = self.lpips(self.img_gen, self.target_image).sum()

        # Additional MSE loss
        if self.mse_strength:
            self.loss += F.mse_loss(self.img_gen, self.target_image) * self.mse_strength

        # Noise regularization
        # reg_loss = self.noise_regularization()
        # self.loss += reg_loss * self.regularize_noise_weight

        # Update params
        self.opt.zero_grad()
        self.loss.backward()
        self.opt.step()

        # Normalize noise
        # self.normalize_noise()

    def get_images(self):
        imgs, _ = self.g_ema(
            [self.latent_in.unsqueeze(0)], input_is_latent=True, noise=self.g_ema.noises)
        return imgs

    def get_latents(self):
        return self.latent_in.detach()


if __name__ == "__main__":

    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=str, required=True)
    parser.add_argument('--output_dir', type=str, required=True)
    parser.add_argument('--gpu', type=int, required=True)
    args = parser.parse_args()

    # Select device
    device = f'cuda:{args.gpu}'

    # Load model
    g = style_gan_2.PretrainedGenerator1024().to(device).train()
    for param in g.parameters():
        param.requires_grad = False

    proj = Projector(g)

    # Load target image
    path = args.input
    if os.path.isdir(path):
        image_files = glob.glob(path + '*.png')
        image_files += glob.glob(path + '*.jpg')
        bool_save_image = False
    else:
        image_files = [path]
        bool_save_image = True

    # Specify save_dir
    save_dir = args.output_dir
    if save_dir[-1] != '/':
        save_dir = save_dir + '/'

    # Project images
    for i, file in tqdm(enumerate(sorted(image_files))):
        print('Projecting {}'.format(file))

        # Load image
        target_image = Image.open(file).convert('RGB')
        transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
        ])
        target_image = transform(target_image).to(device)

        # Run projector
        proj.run(target_image, 2000 if i == 0 else 100)

        # Collect results
        generated = proj.get_images()
        latents = proj.get_latents()
        print(latents.shape)

        # Save results
        save_str = save_dir + file.split('/')[-1].split('.')[0]
        os.makedirs(save_dir, exist_ok=True)
        print('Saving {}'.format(save_str + '_p.png'))
        if bool_save_image:
            save_image(generated, save_str + '_p.png',
                       normalize=True, range=(-1, 1))
        torch.save(latents.detach().cpu(), save_str + '_p.latent.pt')

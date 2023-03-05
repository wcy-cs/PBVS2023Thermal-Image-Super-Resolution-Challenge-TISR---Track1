import torch
from .base_model import BaseModel
from . import networks as N
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from . import losses as L
from pwc import pwc_net
from util.util import get_coord
import numpy as np
import math
from. import base2

# Joint training of LiteISPNet and GCM model for Zurich RAW to RGB (ZRR) dataset

class ZRRJOINTModel(BaseModel):
	@staticmethod
	def modify_commandline_options(parser, is_train=True):
		return parser

	def __init__(self, opt):
		super(ZRRJOINTModel, self).__init__(opt)

		self.opt = opt
		self.loss_names = ['GCMModel_L1', 'LiteISPNet_L1', 'Total']
		self.visual_names = ['dslr_warp', 'dslr_mask', 'data_out', 'GCMModel_out']#, 'dslr']
		self.model_names = ['LiteISPNet', 'GCMModel'] 
		self.optimizer_names = ['LiteISPNet_optimizer_%s' % opt.optimizer,
								'GCMModel_optimizer_%s' % opt.optimizer]

		isp = base2.NAF(opt)#LiteISPNet(opt)
		self.netLiteISPNet= N.init_net(isp, opt.init_type, opt.init_gain, opt.gpu_ids)

		gcm = GCMModel(opt)
		self.netGCMModel = N.init_net(gcm, opt.init_type, opt.init_gain, opt.gpu_ids)

		pwcnet = pwc_net.PWCNET()
		self.netPWCNET = N.init_net(pwcnet, opt.init_type, opt.init_gain, opt.gpu_ids)
		self.set_requires_grad(self.netPWCNET, requires_grad=False)

		if self.isTrain:		
			self.optimizer_LiteISPNet = optim.Adam(self.netLiteISPNet.parameters(),
										  lr=opt.lr,
										  betas=(opt.beta1, opt.beta2),
										  weight_decay=opt.weight_decay)
			self.optimizer_GCMModel = optim.Adam(self.netGCMModel.parameters(),
										  lr=opt.lr,
										  betas=(opt.beta1, opt.beta2),
										  weight_decay=opt.weight_decay)
			self.optimizers = [self.optimizer_LiteISPNet, self.optimizer_GCMModel]

			self.criterionL1 = N.init_net(L.L1Loss(), gpu_ids=opt.gpu_ids)
			# self.criterionSSIM = N.init_net(L.SSIMLoss(), gpu_ids=opt.gpu_ids)
			# self.criterionVGG = N.init_net(L.VGGLoss(), gpu_ids=opt.gpu_ids)

		self.data_ispnet_coord = {}

	def set_input(self, input):
		self.data_raw = input['raw'].to(self.device)# x
		self.data_raw_demosaic = input['raw_demosaic'].to(self.device)
		self.data_dslr = input['dslr'].to(self.device)# ground truth
		self.data_gcm_coord = input['coord'].to(self.device)
		self.image_paths = input['fname']

	def forward(self):
		self.GCMModel_out = self.netGCMModel(self.data_raw_demosaic, self.data_dslr, self.data_gcm_coord)
		self.dslr_warp, self.dslr_mask = \
			self.get_backwarp(self.GCMModel_out, self.data_dslr, self.netPWCNET)
		
		N, C, H, W = self.data_raw.shape
		index = str(self.data_raw.shape) + '_' + str(self.data_raw.device)
		if index not in self.data_ispnet_coord:
			if self.opt.pre_ispnet_coord:
				data_ispnet_coord = get_coord(H=H, W=W)
			else:
				data_ispnet_coord = get_coord(H=H, W=W, x=1, y=1)
			data_ispnet_coord = np.expand_dims(data_ispnet_coord, axis=0)
			data_ispnet_coord = np.tile(data_ispnet_coord, (N, 1, 1, 1))
			self.data_ispnet_coord[index] = torch.from_numpy(data_ispnet_coord).to(self.data_raw.device)
		
		self.data_out = self.netLiteISPNet(self.data_raw, self.data_ispnet_coord[index])

		
		if self.isTrain:
			self.GCMModel_out = self.GCMModel_out * self.dslr_mask
			self.data_out = self.data_out * self.dslr_mask

		else:
			self.dslr_warp, self.dslr_mask = \
			    self.get_backwarp(self.data_out, self.data_dslr, self.netPWCNET)

	def backward(self):
		self.loss_GCMModel_L1 = self.criterionL1(self.GCMModel_out, self.dslr_warp).mean()
		self.loss_LiteISPNet_L1 = self.criterionL1(self.data_out, self.dslr_warp).mean()

		self.loss_Total = self.loss_GCMModel_L1 + self.loss_LiteISPNet_L1 #+ self.loss_LiteISPNet_VGG + self.loss_LiteISPNet_SSIM * 0.15
		self.loss_Total.backward()

	def optimize_parameters(self):
		self.forward()
		self.optimizer_LiteISPNet.zero_grad()
		self.optimizer_GCMModel.zero_grad()
		self.backward()
		self.optimizer_LiteISPNet.step()
		self.optimizer_GCMModel.step()

class GCMModel(nn.Module):
	def __init__(self, opt):
		super(GCMModel, self).__init__()
		self.opt = opt
		self.ch_1 = 32
		self.ch_2 = 64
		guide_input_channels = 8
		align_input_channels = 5
		self.gcm_coord = opt.gcm_coord

		if not self.gcm_coord:
			guide_input_channels = 6
			align_input_channels = 3
		
		self.guide_net = N.seq(
			N.conv(guide_input_channels, self.ch_1, 7, stride=2, padding=0, mode='CR'),
			N.conv(self.ch_1, self.ch_1, kernel_size=3, stride=1, padding=1, mode='CRC'),
			nn.AdaptiveAvgPool2d(1),
			N.conv(self.ch_1, self.ch_2, 1, stride=1, padding=0, mode='C')
		)

		self.align_head = N.conv(align_input_channels, self.ch_2, 1, padding=0, mode='CR')

		self.align_base = N.seq(
			N.conv(self.ch_2, self.ch_2, kernel_size=1, stride=1, padding=0, mode='CRCRCR')
		)
		self.align_tail = N.seq(
			N.conv(self.ch_2, 3, 1, padding=0, mode='C')
		)

	def forward(self, demosaic_raw, dslr, coord=None):
		# demosaic_raw = F.interpolate(demosaic_raw, scale_factor=2, mode="bicubic")
		# coord = F.interpolate(coord, scale_factor=2, mode="bicubic")
		# demosaic_raw = torch.pow(demosaic_raw, 1/2.2)
		if self.gcm_coord:
			# print("demosaic_raw: ", demosaic_raw.shape, " dslr: ", dslr.shape, " coord: ", coord.shape)
			guide_input = torch.cat((demosaic_raw, dslr, coord), 1)
			base_input = torch.cat((demosaic_raw, coord), 1)
		else:
			guide_input = torch.cat((demosaic_raw, dslr), 1)
			base_input = demosaic_raw

		guide = self.guide_net(guide_input)
	
		out = self.align_head(base_input)
		out = guide * out + out
		out = self.align_base(out)
		out = self.align_tail(out) + demosaic_raw
		
		return out

class LiteISPNet(nn.Module):
	def __init__(self, opt):
		super(LiteISPNet, self).__init__()
		self.opt = opt
		ch_1 = 64
		ch_2 = 128
		ch_3 = 128
		n_blocks = 4
		self.pre_ispnet_coord = opt.pre_ispnet_coord

		self.head = N.seq(
			N.conv(3, ch_1, mode='C')
		)  # shape: (N, ch_1, H/2, W/2)

		if self.pre_ispnet_coord:
			self.pre_coord = PreCoord(pre_train=True)

		self.down1 = N.seq(
			N.conv(ch_1+2, ch_1+2, mode='C'),
			N.RCAGroup(in_channels=ch_1+2, out_channels=ch_1+2, nb=n_blocks),
			N.conv(ch_1+2, ch_1, mode='C'),
			N.DWTForward(ch_1)
		)  # shape: (N, ch_1*4, H/4, W/4)

		self.down2 = N.seq(
			N.conv(ch_1*4, ch_1, mode='C'),
			N.RCAGroup(in_channels=ch_1, out_channels=ch_1, nb=n_blocks),
			N.DWTForward(ch_1)
		)  # shape: (N, ch_1*4, H/8, W/8)

		self.down3 = N.seq(
			N.conv(ch_1*4, ch_2, mode='C'),
			N.RCAGroup(in_channels=ch_2, out_channels=ch_2, nb=n_blocks),
			N.DWTForward(ch_2)
		)  # shape: (N, ch_2*4, H/16, W/16)

		self.middle = N.seq(
			N.conv(ch_2*4, ch_3, mode='C'),
			N.RCAGroup(in_channels=ch_3, out_channels=ch_3, nb=n_blocks),
			N.RCAGroup(in_channels=ch_3, out_channels=ch_3, nb=n_blocks),
			N.conv(ch_3, ch_2*4, mode='C')
		)  # shape: (N, ch_2*4, H/16, W/16)

		self.up3 = N.seq(
			N.DWTInverse(ch_2*4),
			N.RCAGroup(in_channels=ch_2, out_channels=ch_2, nb=n_blocks),
			N.conv(ch_2, ch_1*4, mode='C')
		)  # shape: (N, ch_1*4, H/8, W/8)

		self.up2 = N.seq(
			N.DWTInverse(ch_1*4),
			N.RCAGroup(in_channels=ch_1, out_channels=ch_1, nb=n_blocks),
			N.conv(ch_1, ch_1*4, mode='C')
		)  # shape: (N, ch_1*4, H/4, W/4)

		self.up1 = N.seq(
			N.DWTInverse(ch_1*4),
			N.RCAGroup(in_channels=ch_1, out_channels=ch_1, nb=n_blocks),
			N.conv(ch_1, ch_1, mode='C')
		)  # shape: (N, ch_1, H/2, W/2)

		self.tail = N.seq(
			N.conv(ch_1, ch_1*4, mode='C'),
			nn.PixelShuffle(upscale_factor=2),
			N.conv(ch_1, 3, mode='C')
		)  # shape: (N, 3, H, W)   

	def forward(self, raw, coord=None):
		# input = raw
		# input = torch.pow(raw, 1/2.2)
		input = raw
		# print("input: ", input.shape)
		h = self.head(input)
		if self.pre_ispnet_coord:
			pre_coord = self.pre_coord(raw) * 0.1
			pre_coord = torch.clamp(pre_coord, -1, 1)
			pre_coord = pre_coord.unsqueeze(dim=2).unsqueeze(dim=3)
			pre_coord = pre_coord + coord
			h_coord = torch.cat((h, pre_coord), 1)
		else:
			h_coord = torch.cat((h, coord), 1)
		
		d1 = self.down1(h_coord)
		d2 = self.down2(d1)
		d3 = self.down3(d2)
		m = self.middle(d3) + d3
		u3 = self.up3(m) + d2
		u2 = self.up2(u3) + d1
		u1 = self.up1(u2) + h
		out = self.tail(u1)

		return out

class invPixelShuffle(nn.Module):
	def __init__(self, ratio=2):
		super(invPixelShuffle, self).__init__()
		self.ratio = ratio
	def forward(self, tensor):
		ratio = self.ratio
		b = tensor.size(0)
		ch = tensor.size(1)
		y = tensor.size(2)
		x = tensor.size(3)
		assert x % ratio == 0 and y % ratio == 0, 'x, y, ratio : {}, {}, {}'.format(x, y, ratio)
		return tensor.view(b, ch, y // ratio, ratio, x // ratio, ratio).permute(0, 1, 3, 5, 2, 4).contiguous().view(b, -1, y // ratio, x // ratio)


class invUpsampler(nn.Sequential):
    def __init__(self, scale, n_feat, bn=False, act=False):

        m = []
        if (scale & (scale - 1)) == 0:    # Is scale = 2^n?
            for _ in range(int(math.log(scale, 2))):
                m.append(invPixelShuffle(2))
                m.append(nn.Conv2d(in_channels=n_feat*4, out_channels=n_feat, kernel_size=3, padding=1, stride=1))
                if bn: m.append(nn.BatchNorm2d(n_feat))
                if act: m.append(act())
        else:
            raise NotImplementedError

        super(invUpsampler, self).__init__(*m)

class Upsampler(nn.Module):
    def __init__(self, scale, n_feats):
        super(Upsampler, self).__init__()
        m = []
        if (scale & (scale - 1)) == 0:    # Is scale = 2^n?
            for _ in range(int(math.log(scale, 2))):
                m.append(nn.Conv2d(in_channels=n_feats, out_channels=n_feats*4, kernel_size=3, stride=1, padding=1))
                m.append(nn.PixelShuffle(2))

        self.body = nn.Sequential(*m)
        # self.up = nn.PixelShuffle(2)
        # self.conv = nn.Conv2d(in_channels=n_feats, out_channels=n_feats*4, kernel_size=3, stride=1, padding=1)
    def forward(self, x):
        x = self.body(x)
        # print(x.shape)
        # x = self.conv(x)
        # # print(x.shape)
        # x = self.up(x)
        return x


class LiteISPNet1(nn.Module):
	def __init__(self, opt):
		super(LiteISPNet1, self).__init__()
		self.opt = opt
		ch_1 = 64
		ch_2 = 128
		ch_3 = 128
		n_blocks = 4
		self.pre_ispnet_coord = opt.pre_ispnet_coord

		self.head = N.seq(
			N.conv(3, ch_1, mode='C')
		)  # shape: (N, ch_1, H/2, W/2)

		if self.pre_ispnet_coord:
			self.pre_coord = PreCoord(pre_train=True)

		self.down1 = N.seq(
			N.conv(ch_1 + 2, ch_1 + 2, mode='C'),
			N.RCAGroup(in_channels=ch_1 + 2, out_channels=ch_1 + 2, nb=n_blocks),
			N.conv(ch_1 + 2, ch_1, mode='C'),
			invUpsampler(scale=2, n_feat=ch_1),
			# N.DWTForward(ch_1)
		)  # shape: (N, ch_1*4, H/4, W/4)

		self.down2 = N.seq(
			N.conv(ch_1 * 4, ch_1, mode='C'),
			N.RCAGroup(in_channels=ch_1, out_channels=ch_1, nb=n_blocks),
			# N.DWTForward(ch_1)
			invUpsampler(scale=2, n_feat=ch_1),
		)  # shape: (N, ch_1*4, H/8, W/8)

		self.down3 = N.seq(
			N.conv(ch_1 * 4, ch_2, mode='C'),
			N.RCAGroup(in_channels=ch_2, out_channels=ch_2, nb=n_blocks),
			# N.DWTForward(ch_2),
			invUpsampler(scale=2, n_feat=ch_2),
		)  # shape: (N, ch_2*4, H/16, W/16)

		self.middle = N.seq(
			N.conv(ch_2 * 4, ch_3, mode='C'),
			N.RCAGroup(in_channels=ch_3, out_channels=ch_3, nb=n_blocks),
			N.RCAGroup(in_channels=ch_3, out_channels=ch_3, nb=n_blocks),
			N.conv(ch_3, ch_2 * 4, mode='C')
		)  # shape: (N, ch_2*4, H/16, W/16)

		self.up3 = N.seq(
			# N.DWTInverse(ch_2 * 4),
			nn.PixelShuffle(2),
			N.RCAGroup(in_channels=ch_2, out_channels=ch_2, nb=n_blocks),
			N.conv(ch_2, ch_1 * 4, mode='C')
		)  # shape: (N, ch_1*4, H/8, W/8)

		self.up2 = N.seq(
			# N.DWTInverse(ch_1 * 4),
			nn.PixelShuffle(2),
			N.RCAGroup(in_channels=ch_1, out_channels=ch_1, nb=n_blocks),
			N.conv(ch_1, ch_1 * 4, mode='C')
		)  # shape: (N, ch_1*4, H/4, W/4)

		self.up1 = N.seq(
			# N.DWTInverse(ch_1 * 4),
			nn.PixelShuffle(2),
			N.RCAGroup(in_channels=ch_1, out_channels=ch_1, nb=n_blocks),
			N.conv(ch_1, ch_1, mode='C')
		)  # shape: (N, ch_1, H/2, W/2)

		self.tail = N.seq(
			N.conv(ch_1, ch_1 * 4, mode='C'),
			nn.PixelShuffle(upscale_factor=2),
			N.conv(ch_1, 3, mode='C')
		)  # shape: (N, 3, H, W)

	def forward(self, raw, coord=None):
		# input = raw
		# input = torch.pow(raw, 1/2.2)
		input = raw
		# print("input: ", input.shape)
		h = self.head(input)
		if self.pre_ispnet_coord:
			pre_coord = self.pre_coord(raw) * 0.1
			pre_coord = torch.clamp(pre_coord, -1, 1)
			pre_coord = pre_coord.unsqueeze(dim=2).unsqueeze(dim=3)
			pre_coord = pre_coord + coord
			h_coord = torch.cat((h, pre_coord), 1)
		else:
			h_coord = torch.cat((h, coord), 1)

		d1 = self.down1(h_coord)
		d2 = self.down2(d1)
		d3 = self.down3(d2)
		m = self.middle(d3) + d3
		u3 = self.up3(m) + d2
		u2 = self.up2(u3) + d1
		u1 = self.up1(u2) + h
		out = self.tail(u1)

		return out

class PreCoord(nn.Module):
	def __init__(self, pre_train=True):
		super(PreCoord, self).__init__()

		self.ch_1 = 64

		self.down = N.seq(
			N.conv(4, self.ch_1, 3, stride=2, padding=0, mode='CR'),
			N.conv(self.ch_1, self.ch_1, kernel_size=3, stride=2, padding=0, mode='CR'),
			N.conv(self.ch_1, self.ch_1, kernel_size=3, stride=2, padding=0, mode='CR'),
			N.conv(self.ch_1, self.ch_1, kernel_size=3, stride=2, padding=0, mode='CR'),
		)

		self.fc = N.seq(
			nn.Linear(self.ch_1*13*13, 256),
			nn.ReLU(inplace=True),
			nn.Linear(256, 2)
		)
		
		if pre_train:
			self.load_state_dict(torch.load('./ckpt/coord.pth')['state_dict'])

	def forward(self, raw):
		N, C, H, W = raw.size()
		input = raw
		if H != 224 or W != 224:
			input = F.interpolate(input, size=[224, 224], mode='bilinear', align_corners=True)
		
		down = self.down(input)
		down = down.view(N, self.ch_1*13*13)
		out = self.fc(down)
		
		return out
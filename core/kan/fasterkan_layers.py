# .\core\kan\fasterkan_layers.py
from typing import *

import torch
import torch.nn as nn
import torch.nn.functional as F

from core.kan.fasterkan_basis import ReflectionalSwitchFunction, SplineLinear


class FasterKANLayer(nn.Module):
    def __init__(self, input_dim: int, output_dim: int, num_grids: int = 8, *_, **kwargs):
        super().__init__()
        self.layernorm = nn.LayerNorm(input_dim)
        self.rbf = ReflectionalSwitchFunction(num_grids=num_grids, **kwargs)
        self.spline_linear = SplineLinear(input_dim * num_grids, output_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass of the FasterKANLayer.

        This method can handle both 2D and 3D input tensors. If a 2D tensor is provided,
        it is treated as a single sequence (batch_size=1).

        Args:
            x (torch.Tensor): Input tensor of shape (batch_size, seq_len, input_dim)
                              or (seq_len, input_dim).

        Returns:
            torch.Tensor: Output tensor of shape (batch_size, seq_len, output_dim)
                          or (seq_len, output_dim).

        Raises:
            ValueError: If the input tensor has an invalid number of dimensions.
        """
        # Apply layer normalization
        x = self.layernorm(x)

        # Handle different input shapes
        if x.dim() == 2:
            # If input is 2D, add a batch dimension
            x = x.unsqueeze(0)
            squeeze_output = True
        elif x.dim() == 3:
            squeeze_output = False
        else:
            raise ValueError(f"Invalid input dimension. Expected 2D or 3D tensor, got {x.dim()}D.")

        batch_size, seq_len, input_dim = x.shape

        # Apply RBF layer
        spline_basis = self.rbf(x)

        # Reshape for spline linear layer
        spline_basis = spline_basis.view(batch_size * seq_len, -1)

        # Apply spline linear layer
        output = self.spline_linear(spline_basis)

        # Reshape output to match input shape
        output = output.view(batch_size, seq_len, -1)

        # Remove batch dimension if input was 2D
        if squeeze_output:
            output = output.squeeze(0)

        return output


class FasterKAN(nn.Module):
    """
    A network composed of multiple FasterKAN layers.

    Args:
        layers_hidden (List[int]): A list of hidden layer dimensions.
        grid_min (float, optional): The minimum value of the grid for the reflectional switch function. Defaults to -1.2.
        grid_max (float, optional): The maximum value of the grid for the reflectional switch function. Defaults to 0.2.
        num_grids (int, optional): The number of grid points for the reflectional switch function. Defaults to 8.
        exponent (int, optional): The exponent for the reflectional switch function. Defaults to 2.
        inv_denominator (float, optional): The inverse of the denominator for the reflectional switch function. Defaults to 0.5.
        train_grid (bool, optional): Whether to train the grid points of the reflectional switch function. Defaults to False.
        train_inv_denominator (bool, optional): Whether to train the inverse of the denominator for the reflectional switch function. Defaults to False.
        base_activation (Callable, optional): The activation function to apply in the base update path. Defaults to None.
        spline_weight_init_scale (float, optional): The scaling factor for initializing the weights of the spline linear transformation. Defaults to 1.0.
    """

    def __init__(
            self,
            layers_hidden: List[int],
            grid_min: float = -1.2,
            grid_max: float = 0.2,
            num_grids: int = 8,
            exponent: int = 2,
            inv_denominator: float = 0.5,
            train_grid: bool = False,
            train_inv_denominator: bool = False,
            # use_base_update: bool = True,
            base_activation=None,
            spline_weight_init_scale: float = 1.0,
    ) -> None:
        super().__init__()
        self.layers = nn.ModuleList(
            [
                FasterKANLayer(
                    in_dim,
                    out_dim,
                    grid_min=grid_min,
                    grid_max=grid_max,
                    num_grids=num_grids,
                    exponent=exponent,
                    inv_denominator=inv_denominator,
                    train_grid=train_grid,
                    train_inv_denominator=train_inv_denominator,
                    # use_base_update=use_base_update,
                    base_activation=base_activation,
                    spline_weight_init_scale=spline_weight_init_scale,
                )
                for in_dim, out_dim in zip(layers_hidden[:-1], layers_hidden[1:])
            ]
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass of the FasterKAN network.

        Args:
            x (torch.Tensor): The input tensor.

        Returns:
            torch.Tensor: The output tensor.
        """
        for layer in self.layers:
            x = layer(x)
        return x


class BasicResBlock(nn.Module):
    """
    A basic residual block with two convolutional layers and batch normalization.

    Args:
        in_channels (int): The number of input channels.
        out_channels (int): The number of output channels.
        stride (int, optional): The stride of the convolutional layers. Defaults to 1.
    """

    def __init__(self, in_channels, out_channels, stride=1):
        super(BasicResBlock, self).__init__()
        self.conv1 = nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=3,
            stride=stride,
            padding=1,
            bias=False,
        )
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.conv2 = nn.Conv2d(
            out_channels, out_channels, kernel_size=3, stride=1, padding=1, bias=False
        )
        self.bn2 = nn.BatchNorm2d(out_channels)

        self.downsample = nn.Sequential()
        if stride != 1 or in_channels != out_channels:
            self.downsample = nn.Sequential(
                nn.Conv2d(
                    in_channels, out_channels, kernel_size=1, stride=stride, bias=False
                ),
                nn.BatchNorm2d(out_channels),
            )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass of the BasicResBlock.

        Args:
            x (torch.Tensor): The input tensor.

        Returns:
            torch.Tensor: The output tensor.
        """
        identity = self.downsample(x)

        out = F.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out += identity
        out = F.relu(out)

        return out


class SEBlock(nn.Module):
    """
    Squeeze-and-Excitation block for channel attention.

    Args:
        channel (int): The number of input channels.
        reduction (int, optional): The reduction factor for the squeeze operation. Defaults to 16.
    """

    def __init__(self, channel, reduction=16):
        super(SEBlock, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(channel, channel // reduction, bias=False),
            nn.ReLU(inplace=True),
            nn.Linear(channel // reduction, channel, bias=False),
            nn.Sigmoid(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass of the SEBlock.

        Args:
            x (torch.Tensor): The input tensor.

        Returns:
            torch.Tensor: The output tensor.
        """
        b, c, _, _ = x.size()
        y = self.avg_pool(x).view(b, c)
        y = self.fc(y).view(b, c, 1, 1)
        return x * y.expand_as(x)


class DepthwiseSeparableConv(nn.Module):
    """
    Depthwise Separable Convolution.

    Args:
        in_channels (int): The number of input channels.
        out_channels (int): The number of output channels.
        kernel_size (int): The size of the kernel.
        stride (int, optional): The stride of the convolution. Defaults to 1.
        padding (int, optional): The padding of the convolution. Defaults to 0.
    """

    def __init__(self, in_channels, out_channels, kernel_size, stride=1, padding=0):
        super(DepthwiseSeparableConv, self).__init__()
        self.depthwise = nn.Conv2d(
            in_channels,
            in_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            groups=in_channels,
        )
        self.pointwise = nn.Conv2d(in_channels, out_channels, kernel_size=1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass of the DepthwiseSeparableConv.

        Args:
            x (torch.Tensor): The input tensor.

        Returns:
            torch.Tensor: The output tensor.
        """
        x = self.depthwise(x)
        x = self.pointwise(x)
        return x


class SelfAttention(nn.Module):
    """
    Self-attention layer.

    Args:
        in_channels (int): The number of input channels.
    """

    def __init__(self, in_channels):
        super(SelfAttention, self).__init__()
        self.query_conv = nn.Conv2d(in_channels, in_channels // 8, kernel_size=1)
        self.key_conv = nn.Conv2d(in_channels, in_channels // 8, kernel_size=1)
        self.value_conv = nn.Conv2d(in_channels, in_channels, kernel_size=1)
        self.gamma = nn.Parameter(torch.zeros(1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass of the SelfAttention layer.

        Args:
            x (torch.Tensor): The input tensor.

        Returns:
            torch.Tensor: The output tensor.
        """
        batch_size, C, width, height = x.size()
        proj_query = (
            self.query_conv(x).view(batch_size, -1, width * height).permute(0, 2, 1)
        )
        proj_key = self.key_conv(x).view(batch_size, -1, width * height)
        energy = torch.bmm(proj_query, proj_key)
        attention = F.softmax(energy, dim=-1)
        proj_value = self.value_conv(x).view(batch_size, -1, width * height)
        out = torch.bmm(proj_value, attention.permute(0, 2, 1))
        out = out.view(batch_size, C, width, height)
        out = self.gamma * out + x
        return out


class EnhancedFeatureExtractor(nn.Module):
    """
    An enhanced feature extractor with convolutional layers, residual blocks, and self-attention.
    """

    def __init__(self, input_channels: int, hidden_dim: int):
        super(EnhancedFeatureExtractor, self).__init__()
        self.initial_layers = nn.Sequential(
            nn.Conv2d(input_channels, 32, kernel_size=3, stride=1, padding=1),
            nn.ReLU(),
            nn.BatchNorm2d(32),
            nn.Conv2d(32, 64, kernel_size=3, stride=1, padding=1),
            nn.ReLU(),
            nn.BatchNorm2d(64),
            nn.MaxPool2d(2, 2),
            nn.Dropout(0.25),
            BasicResBlock(64, 128),
            SEBlock(128, reduction=16),
            nn.Conv2d(128, 256, kernel_size=3, stride=1, padding=1),
            nn.ReLU(),
            nn.BatchNorm2d(256),
            nn.MaxPool2d(2, 2),
            nn.Dropout(0.25),
            DepthwiseSeparableConv(256, 512, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.BatchNorm2d(512),
            BasicResBlock(512, 512),
            SEBlock(512, reduction=16),
            nn.MaxPool2d(2, 2),
            nn.Dropout(0.25),
            SelfAttention(512),
        )
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Linear(512, hidden_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch_size = x.size(0)
        x = self.initial_layers(x)
        x = self.avg_pool(x)
        x = x.view(batch_size, -1)
        x = self.fc(x)
        return x


class FasterKANvolver(nn.Module):
    """
    A network that combines a convolutional feature extractor with FasterKAN layers for classification.
    """

    def __init__(
            self,
            layers_hidden: List[int],
            input_channels: int,
            hidden_dim: int,
            grid_min: float = -1.2,
            grid_max: float = 0.2,
            num_grids: int = 8,
            exponent: int = 2,
            inv_denominator: float = 0.5,
            train_grid: bool = False,
            train_inv_denominator: bool = False,
            spline_weight_init_scale: float = 1.0,
    ) -> None:
        super(FasterKANvolver, self).__init__()

        # Feature extractor with Convolutional layers
        self.feature_extractor = EnhancedFeatureExtractor(input_channels, hidden_dim)

        # Define the FasterKAN layers
        layers_hidden = [hidden_dim] + layers_hidden  # Add hidden_dim as the first layer
        self.faster_kan_layers = nn.ModuleList(
            [
                FasterKANLayer(
                    in_dim,
                    out_dim,
                    grid_min=grid_min,
                    grid_max=grid_max,
                    num_grids=num_grids,
                    exponent=exponent,
                    inv_denominator=inv_denominator,
                    train_grid=train_grid,
                    train_inv_denominator=train_inv_denominator,
                    spline_weight_init_scale=spline_weight_init_scale,
                )
                for in_dim, out_dim in zip(layers_hidden[:-1], layers_hidden[1:])
            ]
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.feature_extractor(x)
        for layer in self.faster_kan_layers:
            x = layer(x)
        return x

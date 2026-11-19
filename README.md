# XRR 拟合工作台（WGallop Agent 插件）

X 射线镜面反射率（XRR）拟合工作台：层结构建模、Parratt 仿真、差分进化拟合、GenX 项目导入。

## 功能

- 数据导入：文本两/三列、Bruker RAW、GenX 导出的 `.dat`（自动识别列）
- 横坐标单位：q / 2θ / θ 三种，可一键切换
- 层结构：环境 / 分段（每段可重复 N 次）/ 衬底，材料用化学式（支持 `Ni93V7`、`Ni2O3`）
- 物理内核：Parratt 递推 + Nevot–Croce 粗糙度 + 分辨率卷积 + footprint
- 拟合：差分进化，5 种 FOM，参数误差估计（Jacobian 协方差）
- 仪器参数可拟合：波长、I0、本底、分辨率宽度、束宽、样品长、角度偏移
- 输出：拟合曲线 `.dat`/`.csv`、膜层结构参数表（含密度 g/cm³ 与占 bulk 百分比）
- GenX 项目（`.hgx`）导入：解析脚本、参数表、仪器与数据

## 安装（协作者）

### 1. 克隆仓库

```bash
git clone https://github.com/<账号>/xrr-studio.git
```

### 2. 安装后端依赖

插件后端需要 numpy 与 h5py。WGallop 自带的 Python 没有它们，装到插件目录即可：

```bash
python tools/install_deps.py
```

脚本会自动寻找 WGallop 的随包 Python（`%USERPROFILE%\.wgallop\runtimes\plugin-python\*\python.exe`），
把 `numpy`、`h5py` 装进 `vendor/`。也可以用系统 Python：

```bash
python tools/install_deps.py --python "C:\Path\To\python.exe"
```

### 3. 让 WGallop 识别插件

两种方式，任选：

**方式 A：作为开发仓库（推荐，改代码即生效）**

把克隆下来的目录（根目录含 `plugin.json`）设为 WGallop 的**当前工作区**，
软件会自动识别为插件开发仓库，插件列表里显示 dev 标记。
修改文件保存后约 1.6 秒自动重建页面，不需要反复关开插件。

**方式 B：作为普通插件安装**

设置 → 插件管理 → 导入 → 导入本地文件夹，选择该目录。

### 4. 授权后端

首次打开插件会要求确认 Python 后端授权，点允许。
**改了 `backend/` 下任何 `.py` 后，需要重新打开插件并确认一次**（授权绑定源码指纹）。

## 目录结构

```
plugin.json          清单（id / 入口 / 后端 / 权限）
index.html           主页面
assets/app.js        界面与交互
assets/app.css       样式（跟随宿主主题令牌）
backend/core.py      物理内核：SLD、Parratt、分辨率、footprint
backend/fit.py       差分进化与误差估计
backend/hgx.py       GenX 项目文件解析
backend/project.py   项目保存/读取、数据导出
backend/server.py    后端入口（MCP over stdio）
data/materials/      元素散射因子（Cromer-Mann f0、NIST f1/f2、原子量）
tools/install_deps.py 依赖安装脚本
```

`vendor/` 不在仓库里：它是平台相关的二进制依赖，由 `tools/install_deps.py` 生成。

## 物理约定

| 项 | 约定 |
|---|---|
| 层序 | 环境 → 段1 → 段2 … → 衬底（与界面显示一致） |
| 密度 | **g/cm³**（GenX 用 atoms/Å³，导入时自动换算） |
| 粗糙度 | 界面宽度 σ（Å），指该层与**上方层**的界面 |
| 段 | 一个段内所有层构成一个周期，重复 N 次 |
| 分辨率宽度 | 以横坐标单位给出（2θ 或 θ 的度），内部换算到 q |

## 已知限制

- 只做 X 射线镜面反射率，不含中子/磁散射/SXRD/GISAXS
- 单一数据集，无联合拟合
- `vendor/` 为当前平台编译版本，跨平台需重新运行 `install_deps.py`


# HuggingFace 模型下载 —— 本机最优配置与用法

> 本文档基于**本机实测数据**给出配置，不是通用建议。
> 实测日期：2026-10-13

---

## 一、本机实测结果（为什么这么配）

测试目标：`google/embeddinggemma-2/model.safetensors`（1.39 GB）

| 下载方式 | 实测速度 | 评价 |
|---|---|---|
| 直连 `huggingface.co` | **0.01 MB/s** | ❌ 基本不通，几乎无法完成 |
| `hf-mirror.com` 单连接（curl） | 3.85 MB/s | ✅ 可用 |
| `hf-mirror.com` + `hf_xet`（默认） | **0.52 MB/s** | ❌ 反而慢 7 倍 |
| `hf-mirror.com` + 关闭 Xet | 1.75 MB/s | ⚠️ 可用但不够快 |
| **`hf-mirror.com` + 多线程 Range（8 线程）** | **3.65 ~ 3.85 MB/s** | ✅✅ **最快** |
| 同上但 16 线程 | 3.64 MB/s | 无提升（**已达带宽上限**） |

**三条结论：**

1. **必须走 `hf-mirror.com`** —— 直连等于不可用。
2. **必须关闭 Xet** —— `hf_xet` 在 hf-mirror 上会退化（镜像不完整支持 Xet 的 CAS 协议），
   实测慢 3.4 倍，而且还会触发 Windows 符号链接警告。
3. **线程数 8 就够** —— 你的带宽上限约 3.7 MB/s，加到 16 线程没有收益。
   所以对单文件大模型来说，**多线程 HTTP 比任何协议加速都有效**。

> 带宽上限 ≈ 3.7 MB/s（约 30 Mbps）。这是网络条件决定的，不是工具问题。
> 换算参考：1.4 GB 约 6.5 分钟；14 GB 约 65 分钟；70 GB 约 5.3 小时。

---

## 二、已经配好的东西

### 环境变量（用户级，永久生效）

| 变量 | 值 | 作用 |
|---|---|---|
| `HF_HOME` | `D:\HuggingFaceCache` | 模型缓存目录（原有，D 盘空间充足） |
| `HF_ENDPOINT` | `https://hf-mirror.com` | **走国内镜像（最关键的一项）** |
| `HF_HUB_DISABLE_XET` | `1` | **关闭 Xet，实测快 3.4 倍** |
| `HF_HUB_DISABLE_TELEMETRY` | `1` | 关闭遥测请求 |
| `MODELSCOPE_CACHE` | `D:\HuggingFaceCache\modelscope` | ModelScope 缓存也放 D 盘 |

> ⚠️ **环境变量对已经打开的终端不生效，请新开一个终端/重启 PyCharm。**

### PATH

已把 `C:\Users\KK\.conda\envs\densefuse-gpu\Scripts` 加入用户 PATH，
所以新终端里可以直接用 `hf` 和 `modelscope` 命令。

### 已安装的工具

| 工具 | 版本 | 用途 |
|---|---|---|
| `huggingface_hub` | 2.2.0 | 提供 `hf` 命令 |
| `hf_xet` | 已装 | Xet 协议支持（本机已禁用） |
| `modelscope` | 1.40.2 | 阿里魔搭，国内 CDN 备选 |
| **`tools/hfget.py`** | 自写 | **多线程 + 断点续传下载器（本机最快）** |

---

## 三、怎么用（三种场景，按推荐顺序）

### 🥇 场景 1：下载大文件 —— 用 `hfget.py`

**有两种用法，选你喜欢的那种：**

#### 用法 A：直接贴链接（★最省事，适合偶尔下载）

打开 `tools/hfget.py`，找到最下面这段（有醒目注释标记）：

```python
if __name__ == '__main__':
    # ★★★ 最省事的用法：把链接粘在下面，然后直接运行本文件 ★★★
    MODEL_URL = ''                              # ← 把链接粘到这里
    SAVE_DIR = r'D:\models'                     # ← 保存目录
    THREADS = 8                                 # ← 线程数
```

把 `MODEL_URL` 改成你要的链接，然后**直接运行这个文件**（不用带任何参数）：

```powershell
& $py tools\hfget.py
```

或者在 PyCharm 里直接点运行按钮 —— 因为不带参数，就会走这个模式。

> 留空（`MODEL_URL = ''`）时，会自动切换成下面的命令行模式。

#### 用法 B：命令行（适合批量/脚本化）

```powershell
$py = 'C:\Users\KK\.conda\envs\densefuse-gpu\python.exe'
cd D:\project\pythonProject\RegFuse

# ① 直接贴链接（和用法 A 等价，但不用改文件）
& $py tools\hfget.py 'https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors' -o D:\models\gemma

# ② 仓库名 + 文件名
& $py tools\hfget.py google/embeddinggemma-2 model.safetensors -o D:\models\gemma -t 8

# ③ 先看仓库里有哪些文件、多大
& $py tools\hfget.py google/embeddinggemma-2 --list

# ④ 下载整个仓库
& $py tools\hfget.py google/embeddinggemma-2 --all -o D:\models\gemma

# ⑤ 中断了？重跑同一条命令，自动断点续传
```

**链接的写法很宽松，下面这些都能识别：**

```
https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors
https://hf-mirror.com/google/embeddinggemma-2/resolve/main/model.safetensors
https://huggingface.co/google/embeddinggemma-2/blob/main/model.safetensors
https://huggingface.co/google/embeddinggemma-2/blob/main/model.safetensors?download=true
https://hf.co/google/embeddinggemma-2/resolve/main/model.safetensors      （短域名）
google/embeddinggemma-2/resolve/main/model.safetensors                    （不带域名）
```

> **注意**：从浏览器复制链接时，`blob` 和 `resolve` 都可以，脚本会自动转换。
> 子目录的文件（如 `.../resolve/main/subdir/x.bin`）也能正确识别。

#### 它的三个优势

- 多线程分块（Range 请求），本机实测最快
- **断点续传**：中断后重跑自动接着下（已实测验证）
- 只依赖 Python 标准库，不需要装任何东西

### 🥈 场景 2：用 `hf` 命令（官方 CLI，适合下载整个模型目录）

```powershell
# 下载整个模型到指定目录
hf download google/embeddinggemma-2 --local-dir D:\models\gemma

# 下载时排除大文件（只拿配置和分词器）
hf download google/embeddinggemma-2 --local-dir D:\models\gemma --exclude "*.safetensors"

# 只下载 safetensors 权重文件
hf download google/embeddinggemma-2 --local-dir D:\models\gemma --include "*.safetensors"
```

`hf` 也会自动读 `HF_ENDPOINT`，所以同样走镜像。

### 🥉 场景 3：代码里直接加载（不用手动下载）

因为配了 `HF_ENDPOINT`，**代码一行都不用改**，`transformers` 会自动走镜像：

```python
from transformers import AutoModel, AutoTokenizer

# 第一次会自动下载到 D:\HuggingFaceCache，之后直接读缓存
model = AutoModel.from_pretrained("google/embeddinggemma-2")
tok = AutoTokenizer.from_pretrained("google/embeddinggemma-2")
```

### 备选：ModelScope（国内 CDN 最稳）

主流模型（Qwen / Llama / DeepSeek / Mistral / Gemma）都有镜像：

```powershell
modelscope download --model Qwen/Qwen3-27B --local_dir D:\models\qwen3-27b
```

Python API 与 HF 兼容，改一下 import 就能用：

```python
from modelscope import AutoModelForCausalLM   # 替代 transformers
```

---

## 四、常用参数速查

| 需求 | 命令 |
|---|---|
| 看仓库有哪些文件 | `python tools\hfget.py <repo> --list` |
| 下单个大文件（最快） | `python tools\hfget.py <repo> <file> -o <目录> -t 8` |
| 断点续传 | **重跑同一条命令即可** |
| 强制重下 | `python tools\hfget.py <repo> <file> -o <目录> --force` |
| 下整个仓库 | `hf download <repo> --local-dir <目录>` |
| 只下权重 | `hf download <repo> --local-dir <目录> --include "*.safetensors"` |
| 排除权重 | `hf download <repo> --local-dir <目录> --exclude "*.safetensors"` |
| 换镜像端点 | `python tools\hfget.py <repo> <file> --endpoint https://hf-mirror.com` |

---

## 五、排查问题

**① 下载卡住 / 速度异常慢**

先确认环境变量在**当前终端**生效了：

```powershell
echo $env:HF_ENDPOINT      # 应输出 https://hf-mirror.com
```

如果是空的，说明这个终端是在设置变量之前打开的，**新开一个终端**。

**② 想知道下载进度（不信任进度条）**

看磁盘实际增长最可靠：

```powershell
while ($true) {
  $sz = (Get-ChildItem 'D:\HuggingFaceCache' -Recurse -File -EA SilentlyContinue |
         Measure-Object Length -Sum).Sum
  "{0}  {1:N2} GB" -f (Get-Date -Format 'HH:mm:ss'), ($sz/1GB)
  Start-Sleep 10
}
```

**③ 模型文件存在但加载失败**

检查是否有残留的 `.partN` 文件（说明上次没下完）：

```powershell
Get-ChildItem D:\models -Recurse -Filter '*.part*'
```

有的话重跑 `hfget.py` 续传即可。

**④ 速度上不去，想进一步优化**

本机带宽上限实测约 **3.7 MB/s**，已经跑满。进一步提升只能靠网络条件：
- 换网络 / 用代理
- 用 ModelScope 的国内 CDN（有时比 hf-mirror 快）
- 在机房/服务器上下载后拷回来

---

## 六、附：实测验证过的证据

以下都是本次实际跑出来的结果，不是理论推测：

```
目标: google/embeddinggemma-2/model.safetensors  1,488,915,288 bytes (1.39 GB)

方式                                速度
------------------------------------------------
直连 huggingface.co                 0.01 MB/s   （不通）
hf-mirror + hf_xet                  0.52 MB/s
hf-mirror + 关闭 Xet                1.75 MB/s
hf-mirror + 多线程 x4               3.65 MB/s
hf-mirror + 多线程 x8               3.85 MB/s   ← 采用
hf-mirror + 多线程 x16              3.64 MB/s   （无提升=已达带宽上限）

【实测 1】命令行模式，完整下载
  python tools\hfget.py unsloth/... <file> -t 8
  → 1.39 GB，约 8.1 分钟，平均 2.92 MB/s
  → 大小 1,488,915,288 bytes 与服务器一致 ✅

【实测 2】断点续传
  中断时已下 90.75 MB（8 个分块）
  重跑同一命令 → 自动续传 → 最终大小完全一致 ✅
  进度显示精确到 100.0%（不再出现 119%）

【实测 3】贴链接模式（本文档推荐的最新用法）
  python tools\hfget.py 'https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors' -o D:\models -t 8
  → 1.39 GB，462.5 秒，3.09 MB/s
  → 大小 1,488,915,288 bytes 与服务器一致 ✅

【实测 4】脚本内配置模式（改 MODEL_URL 后无参数运行）
  MODEL_URL = 'https://huggingface.co/google/embeddinggemma-2/resolve/main/config.json'
  python tools\hfget.py        （不带参数）
  → 下载成功 ✅

【实测 5】链接解析（各种写法）
  huggingface.co/.../resolve/main/model.safetensors  → repo=google/embeddinggemma-2  file=model.safetensors  rev=main ✅
  hf-mirror.com/.../resolve/main/...                 → ✅
  .../blob/main/model.safetensors                    → ✅
  .../blob/main/model.safetensors?download=true      → ✅
  hf.co/...                                          → ✅
  .../resolve/main/subdir/file.bin                   → file=subdir/file.bin ✅（支持子目录）

文件校验:
  safetensors 头部解析成功，1376 个张量 ✅ 文件有效
```

---

## 七、一句话总结怎么用

> **想下什么，就把浏览器里那条链接粘进 `hfget.py` 的 `MODEL_URL`，然后运行这个文件。完事。**
>
> 不想改文件就用命令行：`python tools\hfget.py '<链接>' -o D:\models`
>
> 断了就重跑，会自己接着下。

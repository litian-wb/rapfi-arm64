# 云端部署（GitHub Codespaces）

把 Rapfi 跑到 GitHub 免费提供的 x86-64 云机器上，手机只负责显示。
好处：服务器 CPU 支持 AVX2/AVX512 + VNNI，比手机 ARM 版快**一个量级**。

## 步骤

1. 在 GitHub 上对这个仓库新建 Codespace（网页 → Code → Codespaces → New）
2. 等 Codespace 打开后，在它的终端里跑：

```bash
bash ~/rapfi-arm64/cloud/setup.sh
```

3. 把 8766 端口设为 public（Codespaces 面板里右键端口 → Port Visibility → Public）
4. 手机端连接地址：

```
wss://<codespace名>-8766.app.github.dev
```

## 注意

- **免费额度每月 60 小时**，用完会按量计费。不用时务必停掉 Codespace。
- Codespace 空闲 30 分钟会自动停止，重新打开后需要再跑一次 setup.sh。
- 引擎按 CPU 指令集自动选版本：avx512vnni > avx512 > avxvnni > avx2 > sse。

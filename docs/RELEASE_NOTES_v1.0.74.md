## fnSoar v1.0.74

### 修复

- **面板完全占满右侧**：Zashboard / Metacubexd 页面 iframe 紧贴侧栏（展开 `201px` / 折叠 `57px` / 窄屏 `0`），消除面板左侧的空白条
- 折叠侧栏后面板同步向左拉伸，不再残留空隙；过渡动画与侧栏一致（0.2s）

### 变更

- 设置页「打开配置目录 / 内核目录」：在 fnOS 微应用窗口内改为经宿主桥直接打开文件管理器并定位到对应目录，宿主桥不可用时自动回退为复制路径
- 新增 fnOS 桌面窗口 SDK `trim-web-app.js` 入库（此前 index.html 已引用但未提交）
- `build.sh` 同步 `engine-start` 的目标路径修正为安装目录顶层 `bin/`（与 `ENGINE_START=TRIM_APPDEST/bin/engine-start` 一致）

### 安装包

| 文件 | 说明 |
|---|---|
| `fnSoar1.0.74.fpk` | 通用包（内置 amd64 + arm64 双内核，推荐） |
| `fnSoar1.0.74-amd64.fpk` | 仅 x86_64 |
| `fnSoar1.0.74-arm64.fpk` | 仅 arm64 |

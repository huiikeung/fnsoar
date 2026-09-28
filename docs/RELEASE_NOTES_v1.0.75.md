## fnSoar v1.0.75

### 打包体系（本次重点）

- **打包脚本迁移到官方 fnpack 规范**：`build_fpk.sh` 改用 `fnpack build` 校验并打包，包结构为官方布局 `{manifest, cmd/, config/, wizard/, ICON.PNG, ICON_256.PNG, app/...}`
- **manifest 改为 platform 声明**：`arch = x86_64 arm64` → `platform = all / x86 / arm`，三种变体沿用通用(双内核)/amd64/arm64 划分
- 新增 `ICON.PNG` / `ICON_256.PNG` 应用图标入库

### 修复

- **升级不再丢配置**：`service-setup` 中安装向导的设置（混合端口 / 外部控制端口 / 订阅链接 / DNS / FakeIP / TUN）改为**仅在首次初始化 config.yaml 时应用**；此前升级或重装时向导默认值会把用户已开启的 tun/dns、已有订阅悄悄覆盖关掉
- **窄屏侧栏右推模式**：窄屏打开侧栏时面板 iframe 左边界跟随侧栏宽度（窄栏 `57px` / 宽栏 `201px`），不再被侧栏遮挡；侧栏由 fixed 悬浮改为占位右推
- **汉堡按钮交互修正**：单击显示窄栏、再次单击收起（侧栏已展开时）、双击显示宽栏；移除「点击外部自动收起」——右推模式下侧栏占据真实布局宽度，收起只通过汉堡或导航跳转完成

### 文档

- 新增 `docs/native-app-migration-plan.md`（fnOS 原生应用迁移计划）

### 安装包

| 文件 | 说明 |
|---|---|
| `fnSoar1.0.75.fpk` | 通用包（内置 amd64 + arm64 双内核，推荐） |
| `fnSoar1.0.75-amd64.fpk` | 仅 x86_64 |
| `fnSoar1.0.75-arm64.fpk` | 仅 arm64 |

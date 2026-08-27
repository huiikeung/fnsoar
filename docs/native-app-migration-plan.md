# fnSoar 原生应用化（fnpack 严格结构）迁移方案

> 依据官方开发文档: [应用框架](https://developer.fnnas.com/docs/core-concepts/framework) ·
> [Native 应用案例](https://developer.fnnas.com/docs/examples/native) ·
> [fnpack CLI](https://developer.fnnas.com/docs/cli/fnpack) ·
> [应用入口](https://developer.fnnas.com/docs/core-concepts/app-entry) ·
> [统一网关](https://developer.fnnas.com/docs/core-concepts/gateway-registration)

## 一、结论

**可以做,而且工作量比想象小。**

fnOS 文档里的「Native 应用」= 以进程方式常驻运行、按官方目录规范 + 生命周期脚本打包的 fpk 应用
(与之相对的只是 Docker 模板应用),**并不是另一套 UI 框架**,桌面入口依然用 iframe 型 `.url` 入口
(官方自带的飞牛便签也是这么做的)。

fnSoar 当前已经符合规范的约 85%,真正要补的只有三类事:

1. **补齐 fnpack 强制校验项**(根目录 `ICON.PNG` / `ICON_256.PNG`);
2. **manifest 字段对齐官方**(`arch` → `platform`);
3. **打包从自研 `build_fpk.sh` 切换为官方 `fnpack build`**(本机已有 v1.2.4)。

## 二、现状对照表

| 官方 Native 要求 | fnSoar 现状 | 状态 |
| --- | --- | --- |
| manifest 基础字段(appname/version/display_name/source…) | 齐全 | ✅ |
| `cmd/main` 处理 start/stop/status,status 退出码 0=运行/3=停止 | 已实现,含 restart/log | ✅ |
| 统一网关入口 `gatewayPrefix` + `gatewaySocket` | `/app/fnnas.fnsoar` + `fnsoar.sock` | ✅ |
| JS SDK 需要 `micro_app=true`(openFileManager 依赖) | 已声明 | ✅ |
| `config/privilege` + `config/resource` 合法 JSON | 有(root + data-share) | ✅ |
| `desktop_uidir=ui`,`desktop_applaunchname` 与入口 ID 一致 | 一致 | ✅ |
| 使用 `TRIM_APPDEST/TRIM_PKGVAR` 等环境变量 | 全程使用 | ✅ |
| 根目录 `ICON.PNG` + `ICON_256.PNG`(fnpack 强制) | **缺失**(只有 `app/ui/images/icon_{64,256}.png`) | ❌ |
| `platform = x86/arm/all` 字段 | 用的是 `arch = x86_64 arm64` | ⚠️ 待对齐 |
| 打包使用 `fnpack build` | 自研 tar 方案 `build_fpk.sh` | ❌ 主要工作 |
| 权限最小化 `run-as: package` | `run-as: root` | ⚠️ 有意保留(TUN 需要) |

## 三、目标目录结构(fnpack 严格布局)

```text
fnsoar/                        # 仓库根即打包目录(fnpbuild --directory .)
├── manifest                   # 对齐 platform 等官方字段
├── ICON.PNG                   # 新增:复制自 app/ui/images/icon_256.png
├── ICON_256.PNG               # 新增:同上
├── LICENSE
├── app/
│   ├── admin/                 # index.html + trim-web-app.js(不动)
│   ├── bin/                   # mihomo-amd64/arm64.real(不动)
│   ├── dashboard/             # zashboard / metacubexd(不动)
│   ├── default-config/        # 内置 config.yaml(不动)
│   └── ui/
│       ├── config             # 入口:已是统一网关标准写法(不动)
│       └── images/icon_{64,256}.png
├── cmd/
│   ├── main                   # start/stop/status/restart/log(已合规)
│   ├── common                 # daemon_status/start_daemon/stop_daemon
│   ├── service-setup          # ★ 顺带修 TUN sed bug
│   ├── start_admin.sh
│   ├── install_init / install_callback
│   ├── upgrade_init / upgrade_callback
│   ├── uninstall_init / uninstall_callback
│   └── config_init / config_callback
├── config/
│   ├── privilege              # run-as root(TUN 必需,注明理由)
│   ├── resource               # port-config + data-share
│   └── clashmini.sc
└── wizard/
    └── install
```

## 四、分阶段实施计划

### 阶段 0 — 基线提交(10 分钟)

- 提交当前未提交的 1.0.74 改动并打 tag `v1.0.74`,推送 origin。
  (沿用约定:动结构前必须有干净回退点。)

### 阶段 1 — 结构补齐(约半天)

1. **新增根目录图标**:复制 `app/ui/images/icon_256.png` → `ICON.PNG` 与 `ICON_256.PNG`。
   `fnpack build` 打包前会强制校验这两项存在。
2. **manifest 对齐**:
   - `arch` → `platform`(见「决策点 1」,建议出包时按变体分别声明);
   - `os_min_version=1.1.8` 维持不变(统一网关已在该版本线上正常工作);
   - 其余字段全部保留。
3. **修复 `cmd/service-setup` 的 TUN sed bug**
   (`s/  enable: true/  enable: false/` 无条件执行导致每次重装静默关闭 TUN)。
   这是升级流程正确性的前提,属于本次必须顺手修掉的项。
4. (可选)`upgrade_callback` 中加入配置保留情况的日志输出,便于排查升级问题。

### 阶段 2 — 打包切换到 fnpack(核心,约半天)

改造 `build_fpk.sh`:

1. 保留现有 staging + 版本号 sed(manifest/app.json/checksum)逻辑;
2. 组装完每个变体的暂存目录后,改为调用:
   ```bash
   fnpack build --directory "${STAGE}"
   ```
   产出 `fnSoar<ver>.fpk` / `-amd64` / `-arm64` 三个变体(与现在一致);
3. `fnpack` 自带打包质检(manifest 必填字段、privilege/resource JSON 合法性、
   ICON.PNG/ICON_256.PNG、app/、cmd/、wizard/、app/ui/ 存在性),
   相当于给发布流程多了一道免费关卡;
4. 验证 checksum 字段是否由 fnpack 生成;若否,保留现有写入逻辑。

### 阶段 3 — 安装验证(1 次完整循环)

```bash
appcenter-cli uninstall fnnas.fnsoar
appcenter-cli install-fpk ./fnSoar<ver>.fpk -e /tmp/fnsoar.env
cp -f /tmp/config.yaml.backup2 "$TRIM_PKGVAR/config.yaml"   # 老规矩:恢复用户配置
curl -s -X POST http://127.0.0.1:9099/api/service \
     -H 'Content-Type: application/json' -d '{"enable":true}'
```

验收清单:

- [ ] 安装成功,应用中心显示启动/停止按钮(ctl_stop=true + main status 正确返回 0/3);
- [ ] 桌面图标经统一网关 `/app/fnnas.fnsoar` 打开;
- [ ] 设置页「打开配置目录」能唤起文件管理器(micro_app + @trimjs/web-app);
- [ ] 重装后 `tun.enable` 仍为 true(sed bug 已修),config.yaml 为用户原版;
- [ ] zashboard / metacubexd 双面板可访问;
- [ ] `fnpack build` 三变体产物大小与现版本相当(~56M/40M/38M)。

## 五、风险与决策点

1. **platform vs arch(唯一不确定项)**
   官方文档只有 `platform=x86/arm/all`;当前包用 `arch` 且能装。
   建议:amd64 变体 → `platform=x86`,arm64 变体 → `platform=arm`,
   通用包(内置双架构二进制、脚本运行时选择)先试 `all`,
   若 appcenter 拒绝则回退为「arch + platform 双字段并存」。试装一次即可定论。
2. **run-as root 保留**
   官方便签等应用用 `run-as: package` 最小权限;但 mihomo TUN 需要创建 utun 设备、改路由表,
   必须 root。保留并在 README 注明原因,不做变更。
3. **原地升级**
   此前 micro_app 变更必须卸载重装(注册信息在安装时落库)。本次 micro_app 已随包声明且不再变化,
   之后应可走应用中心原地升级(upgrade_init/callback 路径),阶段 3 顺带验证。
4. **service_port=9090** 是 mihomo 外部控制端口而非 UI 端口;入口走统一网关后
   `protocol/port` 不参与路由,维持现状即可。
5. **零功能风险**:admin_server.py、index.html、引擎、面板全部不动,
   本次只动「壳」(图标/manifest 字段/打包脚本/service-setup 一行 bug)。

## 六、预估工作量

半天 ~ 1 天(含一次完整的卸载 → 安装 → 验证循环与 config.yaml 恢复)。

---

## 七、执行结果(v1.0.75 已安装验证)

| 验收项 | 结果 |
| --- | --- |
| fnpack build 三变体 | ✅ 通用(all/57M)、amd64(x86/40M)、arm64(arm/38M),双内核校验通过 |
| `platform=all` 接受度 | ✅ appcenter 正常识别(决策点 1 关闭,无需回退 arch) |
| 配置保留 + TUN 修复 | ✅ 卸载→重装后 config.yaml 与备份 diff 完全一致,`tun.enable: true` 未被篡改 |
| 官方框架落位 | ✅ `/var/apps/fnnas.fnsoar/{manifest,cmd,config,wizard}` + `target→@appcenter` 软链 |
| `cmd/main status` | ✅ TRIM_* 环境下 exit=0(running);应用中心列表显示 running |
| 引擎与 UI | ✅ mihomo v1.19.30 运行;`/api/version` app=1.0.75;SDK MIME=application/javascript;fnsoar.sock 就绪 |

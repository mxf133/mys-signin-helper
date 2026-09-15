# 米游社签到助手

原神 + 崩坏：星穹铁道（国服 · 米游社账号）每日自动签到工具，带图形界面。

**不需要开游戏**，登录一次之后就能一键把两个游戏一起签了，也可以交给
Windows 计划任务每天在后台静默完成，奖励直接进游戏邮箱。

![界面预览](screenshot.png)

> 截图中的 UID 与昵称为演示数据。

---

## 特性

- **图形界面** —— 每个游戏一张状态卡，实时显示今日签到、本月累计、上次查询时间
- **一次登录管两个游戏** —— 米游社 cookie 是账号级的，登录一次即可
- **免手动复制 cookie** —— 内嵌拉起系统自带的 Edge 完成登录，从浏览器引擎层读取
  cookie（含 HttpOnly），不用去 F12 里翻
- **凭据本地加密** —— 用 Windows DPAPI 加密保存，只有当前 Windows 用户能解密
- **定时签到** —— 一键注册 Windows 计划任务，用 `pythonw.exe` 执行，不弹黑窗
- **高分屏适配** —— 声明 DPI 感知 + 按真实缩放渲染，4K 屏上字体不糊
- **零额外浏览器** —— 复用系统 Edge（Playwright `channel="msedge"`），
  不下载 Chromium

---

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11 |
| Python | 3.10 或更高（[python.org](https://www.python.org/downloads/) 版本，安装时勾选 *Add Python to PATH*） |
| 浏览器 | Microsoft Edge（系统自带即可） |
| 账号 | 米游社账号，且账号下有对应游戏的角色 |

> 目前只支持国服（米游社 / `docs.qq.com` 之外的米哈游国服接口）。国际服 HoYoLAB
> 需要另一套凭据，暂未适配。

---

## 安装

### 方式一：下载后一键安装（推荐）

1. 点右上角 **Code → Download ZIP**，解压到任意目录（路径不要有中文以外的特殊字符）
2. 双击 **`安装依赖.bat`** —— 会自动创建 `.venv` 并装好依赖
3. 双击 **`启动米游社签到助手.bat`** 打开界面

### 方式二：Git 克隆

```bat
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>
安装依赖.bat
```

### 方式三：手动装

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\pythonw.exe app.pyw
```

---

## 使用

| 步骤 | 操作 |
| --- | --- |
| 1 | 打开程序，点 **【登录米游社】** |
| 2 | 在弹出的 Edge 窗口里正常登录（扫码 / 验证码都行），登录成功后窗口会自动关闭 |
| 3 | 点 **【一键全部签到】** 验证一次 |
| 4 | 想每天自动签到：填好时间（默认 `09:05`）后点 **【开启 / 更新】** |

之后每天到点由 Windows 计划任务在后台把两个游戏都签一遍。

---

## 界面说明

**每个游戏一张卡片**，显示：

- 绑定的角色 —— 昵称 + UID
- **今日签到** —— 已签到 / 未签到 / 签到失败 / 需重新登录（左侧圆点同步变色）
- **本月累计** —— 本月已签到天数
- **上次查询** —— 最近一次查询时间
- **单独签到** —— 只签这一个游戏

**操作区按钮：**

| 按钮 | 作用 |
| --- | --- |
| 一键全部签到 | 依次签原神和崩铁（已签过的会提示"今天已经签到过了"） |
| 刷新状态 | 只查询两个游戏的签到状态，不重复签 |
| 登录米游社 | 打开 Edge 完成登录；已有凭据时会提示是否覆盖 |
| 清除登录凭据 | 删除本机保存的登录信息 |
| 开启 / 更新 | 注册（或更新时间）Windows 计划任务，两个游戏一起签 |
| 关闭 | 删除计划任务 |
| 清空 / 打开日志文件 | 日志区操作 |

**运行日志** —— 每一步操作的完整记录，也会写入 `signin.log`。

---

## 命令行用法

计划任务和进阶用法可以直接调 `cli.py`：

```bat
.venv\Scripts\python.exe cli.py login                  :: 登录一次
.venv\Scripts\python.exe cli.py sign                   :: 签到全部游戏
.venv\Scripts\python.exe cli.py sign --game genshin    :: 只签原神
.venv\Scripts\python.exe cli.py sign --game starrail   :: 只签崩铁
.venv\Scripts\python.exe cli.py status                 :: 查询状态
.venv\Scripts\python.exe cli.py task                   :: 注册计划任务
.venv\Scripts\python.exe cli.py untask                 :: 删除计划任务
```

`--game` 取值：`all`（默认）、`genshin`（原神）、`starrail`（崩坏：星穹铁道）。

---

## 目录结构

**仓库里的文件：**

```
├─ app.pyw                  图形界面主程序
├─ core.py                  核心逻辑（登录 / 多游戏签到 / 计划任务 / DPAPI）
├─ cli.py                   命令行入口（计划任务调用的就是它）
├─ 启动米游社签到助手.bat      启动界面
├─ 安装依赖.bat              创建 .venv 并安装依赖
├─ requirements.txt         依赖清单
├─ icon.ico / icon.png      程序图标（16~256px 七档）
├─ screenshot.png           界面预览
├─ LICENSE
└─ .gitignore
```

**首次运行后会在本目录生成（已被 `.gitignore` 排除，不会提交）：**

```
├─ .venv\                   Python 运行环境
├─ cookie.enc               加密后的登录凭据
├─ .edge_profile\           Edge 登录会话缓存
├─ signin.log               运行日志
├─ state.json               各游戏最近一次签到状态
└─ settings.json            界面设置（自动签到时间）
```

---

## 实现要点

- **登录**：用 Playwright 驱动系统自带的 Edge 打开米游社登录页，你正常登录后
  从浏览器引擎层读取 cookie（HttpOnly 的也能拿到），再加密保存。
  与 Snap Hutao 内嵌 WebView 的思路一致，且不额外下载 Chromium。
- **多游戏**：`core.py` 里的 `GAMES` 表声明支持的游戏，对应的接口头是
  `x-rpc-signgame: hk4e`（原神）和 `hkrpg`（崩铁）。整个流程只建一个
  `genshin.Client`，一次登录、一次遍历把两个游戏签完。
  **想加新游戏只需往 `GAMES` 表里加一行**（`genshin.py` 还支持崩坏3、绝区零）。
- **凭据存储**：`cookie.enc`，使用 Windows DPAPI 加密，只有当前 Windows 用户
  能解密，换用户或换机器都无效。
- **定时**：注册名为 `MYS-DailySignIn` 的 Windows 计划任务，用 `pythonw.exe`
  执行，因此不会弹黑色命令行窗口。
- **高分屏适配**：启动时先声明 DPI 感知（`SetProcessDpiAwarenessContext`，
  失败则退到 `shcore` / `user32` 的老接口），再按真实 DPI 设置 `tk scaling`
  并用 `px()` 换算所有布局像素。不这么做的话，Windows 会把整个窗口当位图
  放大，4K 屏上字体边缘会被插值糊掉。缩放比例改变后无需改代码，自动适配。
- **环境自举**：`app.pyw` 顶部会检查当前解释器，如果不是项目自带的 `.venv`
  就自动用 `pythonw.exe` 重跑一遍，所以直接双击 `.pyw` 也不会缺依赖。

---

## 隐私与安全

- **凭据只存本机**：cookie 经 Windows DPAPI 加密后写入 `cookie.enc`，
  只有当前 Windows 用户能解密，不会上传到任何第三方服务器。
- **本仓库不含任何凭据**：`.gitignore` 已排除 `cookie.enc`、`.edge_profile/`、
  `signin.log`、`state.json`、`settings.json`，克隆下来的是干净的代码。
- **不碰游戏**：只调用米游社官方签到接口，不修改游戏文件、不注入游戏进程。

---

## 常见问题

**Q：界面字体发虚 / 模糊？**
程序会声明 DPI 感知并按真实缩放渲染。如果仍然发虚，检查两点——① Windows
设置里显示缩放是不是非整数倍（如 125%、175%），可改成 100% / 150% / 200%；
② 系统"调整 ClearType 文本"是否开启（设置 → 显示 → 高级显示 → 文本渲染，
或运行 `cttune`）。

**Q：签到提示"cookie 已失效"？**
cookie 有有效期（通常一个月左右）。重新点一次【登录米游社】即可，其余不变。
这是这类工具唯一的日常维护点。

**Q：只有原神能签，崩铁显示"签到失败"？**
先确认该米游社账号下确实有对应游戏的角色。可以点【刷新状态】看卡片右上角有没有
显示 UID——没有 UID 说明这个账号在这款游戏下没有角色。

**Q：提示签到失败、或者提示需要人机验证？**
米哈游有小概率触发 Geetest 人机验证，脚本端无法通过。手动去米游社 App / 网页
签一次即可，第二天通常就恢复正常。

**Q：换了电脑 / 重装了系统？**
`cookie.enc` 依赖当前 Windows 用户的密钥，换环境后必须重新登录一次。
新机器上先双击 `安装依赖.bat` 重建环境。

**Q：想改自动签到时间？**
界面上改时间后点【开启 / 更新】即可覆盖旧任务。

**Q：能加别的游戏吗？**
可以。`genshin.py` 还支持崩坏3、绝区零等，在 `core.py` 的 `GAMES` 表里
加一行（key / 显示名 / `Game` 枚举成员名 / `x-rpc-signgame` 值）即可，
界面会自动生成对应的卡片和按钮。

**Q：怎么换程序图标？**
替换 `icon.ico` 和 `icon.png` 两个文件（保持文件名不变），然后重建桌面快捷方式
（如果建了的话）。桌面图标没刷新就按一下 F5。

---

## 免责声明

- 本工具仅供学习与个人使用，自动化签到在理论上属于用户协议的灰色地带，
  **请仅用于你自己的单个账号，不要用于多账号或高频请求**。
- 使用本工具产生的任何后果由使用者自行承担，作者不对账号异常等情况负责。
- 米哈游官方接口如有变更，本工具可能失效，需等待依赖库或本仓库跟进更新。

---

## License

[MIT](LICENSE)

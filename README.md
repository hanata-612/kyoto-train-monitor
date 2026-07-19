# 京都电车运行异常监控（TaxiLogger 配套）

电车停运 → 振替輸送 → 车站前出租车需求激增。这个监控每 10 分钟检查一次京都相关线路，异常发生和恢复的瞬间推送到你的 iPhone（ntfy）+ 邮箱。

## 监控范围

| 数据源 | 线路 | 可靠性 |
|---|---|---|
| JR西日本 运行信息 JSON | JR京都線・湖西線・奈良線・嵯峨野線・琵琶湖線・草津線・山陰線(京都口)・大和路線 | 高（结构化数据，含**振替輸送标志**） |
| 阪急官网页面 | 全线（京都本線含む） | 实验性（关键词扫描） |
| 京阪官网页面 | 全线 | 实验性 |
| 近铁官网页面 | 全线（京都線含む） | 实验性 |

实验性 = 页面无法判定时**只记日志、不报警**（宁漏勿误）。跑两周后看 Actions 日志里的 unknown 记录，再针对性调整解析规则。京都市营地下鉄暂未纳入（故障率低、无易用数据源），列为 v2。

## 部署步骤（约 15 分钟）

### ① iPhone 装 ntfy

1. App Store 搜 **ntfy** 安装（免费开源）
2. 打开 App → Add subscription → 输入一个**别人猜不到的频道名**，比如 `taxilogger-kyoto-a8x3k`（频道名就是密码，别用简单词）
3. 记住这个频道名，下面要用

### ② 建 GitHub 仓库

1. 注册/登录 github.com
2. New repository → 名字随意（如 `kyoto-train-monitor`）→ **Public**（公开仓库 Actions 免费不限量；私有仓库每月 2000 分钟，每 10 分钟跑一次会超）→ Create
3. 把本压缩包里的三个文件按原目录结构上传：
   - `monitor/kyoto_train_monitor.py`
   - `.github/workflows/train-monitor.yml`
   - `README.md`（可选）
   
   网页上传：Add file → Upload files，注意 `.github/workflows/` 目录要建对（可以先 Create new file 输入路径 `.github/workflows/train-monitor.yml` 粘贴内容）

### ③ 配置 Secrets

仓库 Settings → Secrets and variables → Actions → New repository secret：

| Name | Value |
|---|---|
| `NTFY_TOPIC` | 你在①里定的频道名 |
| `NOTIFY_EMAIL` | 你的邮箱（可选；不设就只推 ntfy） |

频道名放 Secret 里，所以仓库公开也没人能给你发推送。

### ④ 启用并测试

1. 仓库 Actions 标签页 → 如提示则点 Enable workflows
2. 左侧选 kyoto-train-monitor → Run workflow（手动触发一次）
3. 看运行日志：绿色✅ + 末尾 `完成: 当前异常 N 件` 即部署成功
4. 之后每 10 分钟自动运行

### ⑤ 测试推送（可选但推荐）

手机 ntfy 里能不能收到，不用等真的停运——在电脑浏览器地址栏访问不了，用任意 HTTP 工具或让手机上的 ntfy App 自己发测试消息（订阅页右上角 Send test notification）。

## 推送长这样

```
🚨 JR西日本 JR京都線: 運転見合わせ
原因: 人身事故
🔁 振替輸送実施中 → 车站前出租车需求可能激增！
关注车站: 京都駅・山科駅
18:42 JST
```

恢复时会收到 ✅ 通知，深夜 1:00-4:30 JST 静默。

## 已知限制

- GitHub Actions 的定时任务可能延迟 3-10 分钟，高峰期偶尔跳过一轮——对"停运通常持续 30 分钟以上"的场景够用
- ntfy 免费版的邮件转发每天有条数限制（推送本身不限）；异常本来就少，一般够用
- JR 数据来自其走行位置服务的非公开接口，官方改版时需要跟进（脚本会连续失败 3 次后推送"数据源异常"提醒你）
- 私铁三家是页面关键词扫描，可能漏报（不会误报）；调优需要积累日志

## 维护

- 想增减 JR 监控线路：改脚本里的 `JR_LINE_NAMES`（完整 key 对照在脚本注释）
- 想关深夜静默：workflow 的 env 里加 `QUIET: off`
- 数据源失效：Actions 日志会显示失败，且连续 3 次后你会收到 📡 推送

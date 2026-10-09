# TV 影视源自动体检 · AutoBuild Source

给 [iptvorganization/TV-AutoBuild](https://github.com/iptvorganization/TV-AutoBuild) 这类 **FongMi / TVBox 内核** 的影视 APK，提供一个**每天自动检测源是否失效、自动剔除死源、自动重新排序并发布**的接口文件。

你只需要把一个 URL 粘贴进 APK，剩下的交给 GitHub Actions。

```
config/upstreams.json 你订阅的上游接口清单
        │
        ▼
scripts/sync_upstream.py   拉取 → 识别格式 → 去重合并（打 _from 标记）
        │
        ▼
config/sources.json   你维护的候选源（收养进来的站点/解析/直播）
        │
        ▼
scripts/autobuild.py  深度验证（三闸门） → 更新历史 → 打分 → 排序 → 剔除连续失效源
        │
        ├── state/history.json    每个源的滚动健康档案（10 次窗口）
        ├── state/report.json     本次检测原始结果
        └── dist/tvbox.json       ★ 给 APK 用的接口文件
            dist/report.md        人读的日报
        │
        ▼
.github/workflows/daily.yml   每天定时跑 + commit + 发布到 GitHub Pages
```

---

## 一、这个 APK 要填什么地址

TV-AutoBuild 打包出来的是 FongMi 内核的 APK（`TV-mobile-*.apk` / `TV-leanback-*.apk`），
它读取的是标准 **TVBox / FongMi 接口 JSON**：

```json
{ "spider": "...", "sites": [...], "parses": [...], "lives": [...], "flags": [...] }
```

在 APK 里填地址的位置：

> 打开应用 → **设置** → **其他 / 接口** → **配置地址**（不同版本叫法略有差别，通常在「设置 → 其他 → 自定义接口地址」）→ 粘贴 URL → 确定。

可填的地址（部署完成后会得到）：

| 地址 | 说明 |
| --- | --- |
| `https://<你的用户名>.github.io/<仓库名>/tvbox.json` | **推荐**，走 GitHub Pages，国内通常可直连 |
| `https://raw.githubusercontent.com/<用户名>/<仓库名>/main/dist/tvbox.json` | 备用，部分地区可能被墙 |

> 想要更稳，也可以把 `dist/tvbox.json` 丢到任意 OSS / CF Workers 里再填那个地址。

---

## 二、快速开始

### 1. 建仓库并推上去

```bash
git clone <你的 fork 或本地目录>
cd tv-source-autobuild
git init && git add -A && git commit -m "init"
git remote add origin https://github.com/<你>/<仓库名>.git
git push -u origin main
```

### 2. 把你手上的源“收养”进来（自动适配）

如果你已经有一堆 TVBox 接口地址，不用手抄，直接导：

```bash
python scripts/import_sources.py https://example.com/tvbox.json
python scripts/import_sources.py ./backup.json --spider   # 同时采用它的 spider jar
python scripts/import_sources.py https://example.com/tvbox.json --dry-run   # 只看统计
```

它会解析 `sites` / `parses` / `lives`，按 `key` / `name` 去重后合并进 `config/sources.json`，
容忍尾逗号、`//` 与 `/* */` 注释、BOM。**有 `api` / `ext` / `jar` / `url` 任意一个地址字段的条目都会被收养**
（`api: "csp_Xxx"` 这种走公共 jar 的站点也算，它会被标为「无法验证」而不是丢弃）；只有 `playUrl`、或者什么都没有的残缺条目会被跳过。

也可以直接手工编辑 `config/sources.json`（结构见下一节）。

### 3. 订阅上游（每日自动同步）

上面那步是**一次性导入**；如果你有个会持续更新的上游接口，用 `config/upstreams.json` 订阅它：

```bash
python scripts/sync_upstream.py               # 拉取所有 enabled 的上游并合并
python scripts/sync_upstream.py --dry-run     # 只看统计，不写入
python scripts/sync_upstream.py --only 名字    # 只同步某一个
```

```jsonc
// config/upstreams.json
{
  "upstreams": [
    {
      "name": "my-upstream",
      "url":  "https://.../tvbox.json",   // 也支持 api.github.com/repos/.../contents/...
      "enabled": true,
      "format": "auto",    // auto | tvbox | lunatv
      "mode":   "merge",   // merge | replace
      "timeout": 20
    }
  ]
}
```

- **`format`** —— `auto` 自动识别。`tvbox` 是标准 `sites/parses/lives`；`lunatv` 是
  `{"cache_time":…, "api_site": {条目名: {name, api, detail}}}` 这种映射表（**不是** TVBox 格式，
  直接喂 `import_sources.py` 会一条都读不到），适配器会把它转成 `type: 0` 的采集站。
- **`mode: merge`**（默认）—— 取并集，你本地手动加的源不会被上游覆盖。
  **`mode: replace`** —— 完全跟随上游：先丢掉上次从这个上游收养的条目（按 `_from` 标记识别）再合并，
  所以上游改了 key 或删了源，你这边会同步。
- 每个被收养的条目都会打上 **`_from`**（以及上游里带的 `_note`）—— 下划线前缀字段生成输出时会被剥掉，
  所以既能查「这个源哪来的」，又不会污染 `dist/tvbox.json`。
- **单个上游挂掉只打印 `[fail]` 不中断**，当天的深度体检照常跑。要让它卡住流水线就加 `--strict`。
- 拉取状态写在 `state/sync.json`。

流水线里的顺序是 **sync → 深度体检 → 提交**，所以**上游塞进来的死源当次就会被闸门拦住，
连续 3 天不活才被踢出输出**——同步只管搬运，取舍交给已有的体检。

### 4. 本地跑一次

```bash
python scripts/autobuild.py            # 检测 + 生成
python scripts/autobuild.py --check    # 只检测
python scripts/autobuild.py --build    # 只用上次的检测结果重新生成
```

产物：`dist/tvbox.json`（接口）、`dist/report.md`（日报）、`state/history.json`（健康档案）。

> 本机没有 Python 也可以：把仓库推上去后直接在 **Actions → Daily source check → Run workflow** 手动跑一次。

### 5. 打开自动化

1. **Settings → Actions → General → Workflow permissions** → 选 *Read and write permissions*（否则 commit 会被拒）。
2. **Settings → Pages → Source** → 选 **GitHub Actions**（否则 Pages 部署那步会报错）。
3. 推一次代码，`Actions` 里会自动出现 `Daily source check`，默认**每天 17:37 UTC（北京时间次日 01:37）**跑一次。

跑完后，`dist/tvbox.json` 会被 commit 并发布，`Actions` 的 **Summary** 页会直接显示当天的检测日报表格。

---

## 三、`config/sources.json` 结构

```jsonc
{
  "schema": 1,

  "settings": {
    "timeout": 10,          // 单次请求超时（秒）
    "workers": 8,           // 并发检测线程数
    "drop_after": 3,        // 连续失效 N 次才从输出里剔除（防抖动）
    "keyword": "中国",       // 采集站搜索验证用的关键词

    "play_check": "auto",   // 闸门3 播放链路验证：auto / strict / off
    "play_check_urls": 2,   // 最多试几条直链（第一条挂了换下一条）

    "parse_test_url": ""    // 填了就对解析接口做“真解出”深度验证
  },

  "app": {
    // 这一层会原样写进输出 JSON 的顶层，按需增删
    "spider": "",           // 公共爬虫 jar 地址
    "wallpaper": "...",
    "flags": ["qq", "qiyi", "youku", "..."],
    "doh": [ ... ],
    "ads": [],
    "hosts": {}
  },

  "sites":   [ ... ],       // 点播站点
  "parses":  [ ... ],       // 解析接口
  "lives":   [ ... ]        // 直播源
}
```

### 候选源里的“注释字段”

任何以 `_` 开头的字段（`_note`、`_comment`…）**只在候选池里有效，生成输出时会被自动剥掉**，
所以你可以放心给每个源写备注。

### 每个 `sites` 条目

就是标准的 TVBox/FongMi 站点对象，外加一个**可选**的 `probe`：

```jsonc
{
  "key": "demo",
  "name": "演示站",
  "type": 0,                          // 0/3 采集站, 1 规则站, 6 蜘蛛站
  "api": "https://x.com/api.php/provide/vod/from/xxx/at/json",
  "ext": "...",                       // 字符串 URL 或 {"api": "..."}
  "jar": "https://x.com/custom_spider.jar",
  "playUrl": "https://x.com/player/?url=",   // 站点级播放器（fongmi/TV Site.java 认这个字段）
  "searchable": 1, "quickSearch": 1, "filterable": 1, "changeable": 1,

  "_note": "写给自己的备注，不会进输出",

  "probe": {                          // 可选：不想让脚本猜就写死
    "kind": "cms",                    // cms | http | json | m3u8
    "url":  "https://x.com/api.php/provide/vod/"
  }
}
```

**没写 `probe` 时的自动推断：**

| 情况 | 探测方式 |
| --- | --- |
| `api` 是 URL，且 `type ∈ {0,3,6}` **或**路径含 `provide/vod` | **采集站三闸门深度验证**（见下） |
| `ext` 是带 `provide/vod` 的 URL | 同上，按采集站深度验证 |
| `ext` 是 `.json` URL | 下载并要求能解析成带列表的 JSON |
| `ext` 是其它 URL / `jar` 是 URL | HTTP 探活 + 体积/内容校验（`jar` 太小视为失效） |
| **`playUrl` 是 URL** | GET 可达性探测（播放器网关带空 `?url=` 也会 200，所以只验可达） |
| `api` 是 `csp_Xxx` 这种类名、没有任何 URL | **标记为「无法验证」**，永不因检测而被剔除 |

**采集站三闸门 —— 从「接口活着」一路验到「真的能看」：**

| 闸门 | 做什么 | 不过会怎样 |
| --- | --- | --- |
| **1 · 目录** | `?ac=list&pg=1` → 要求返回**非空条目**且不是 HTML | 判失效 |
| **2 · 搜索** | `?ac=detail&wd=关键词` → 要求**真能搜到东西** | 判失效 |
| **3 · 播放链路** | 从搜索结果里取出真实 `vod_play_url` → 解析出 HLS 直链 → 拉播放列表 → **首个分片真的能下载**（且不是 HTML） | 判失效（`play_check=auto/strict`） |

闸门 3 具体做的事：

```
vod_play_url "标题$链接#标题$链接$$$线路2$..."
      → 分类：含 m3u8 的按 HLS 走；.mp4/.mkv 等按直链走；播放器页跳过
      → HLS：主播放列表 → 变体 → 媒体播放列表 → GET 首个分片（限 64KB）
      → 分片 403/401 时，带同源 Referer 重试一次（CDN 防盗链）
      → 直链：GET 前 64KB，要求非空且不是 HTML
```

三种模式：

| `play_check` | 行为 |
| --- | --- |
| `auto`（默认） | 目录里**有**直链 → 必须验通；**没有**直链（全靠解析接口）→ 标「播放链路未验证（无直链）」，不判死 |
| `strict` | 没有直链也判失效。适合你只想要「开箱即播」的源 |
| `off` | 关闭闸门 3，退回到前两闸门 |

> 闸门 3 每站最多多花 `2 × play_check_urls` 次请求（默认 2 条候选 × 列表+分片）。
> CDN 偶尔抖动会被 `drop_after = 3` 吸收，不会一天就把好源踢掉。

> **分类按 URL 路径判断，不只看 `type`** —— 所以 `type: 1` 配 `.../api.php/provide/vod/...`
> 这种写法依然会走深度验证，不会退化成只探首页 200。

任何一个字段挂了，整站就判失效；日报的「其它地址」列会精确指出是 `api` / `ext` / `jar` / `playUrl` 里的哪一块挂了。

> 换句话说：**深度验证**针对的是绝大多数的采集站（真正决定“源死没死”的那一层），
> 而不是只看首页 200。

### `parses` 与 `lives`

```jsonc
"parses": [{ "name": "示例解析", "type": 0, "url": "https://x.com/parse?url=" }]
"lives":  [{ "name": "示例直播", "type": 0, "url": "https://x.com/live.m3u8" }]
```

- `lives`：下载播放列表，要求 `#EXTM3U` 开头，统计频道数。
- `parses`：默认只做可达性探测；在 `settings.parse_test_url` 里填一个真实视频详情页地址后，
  会请求 `url + 该地址`，并要求返回的 JSON 里**真的解出了播放地址**。

---

## 四、“失效”是怎么判定的（防抖动设计）

一天的数据不作数，靠滚动历史：

| 状态 | 含义 | 是否进输出 |
| --- | --- | --- |
| `alive` | 本次探测通过 | ✅ 排最前 |
| `unverified` | 没有可探测地址（`csp_*` 类名站） | ✅ 其次 |
| `grace` | 本次失败，但连续失败次数 `< drop_after` | ✅ 排最后（暂时保留） |
| `dead` | 连续失败 `≥ drop_after`（默认 3 天） | ❌ 剔除 |

同一状态内按**得分**排序，得分由这些因素决定：

```
100
 - min(延迟ms / 100, 30)          越快越好
 + min(条目数, 20) * 0.5           目录越丰富越好
 - 最近 10 次的失败率 * 25          越稳定越好
 - 连续失败次数 * 10                越可靠越好
```

所以 `dist/tvbox.json` 里的顺序 = **又快、又全、又稳的源排在 APK 最前面**。

死掉的源不会从 `config/sources.json` 里删除——它只是被暂时踢出输出，
恢复后下一次检测会自动加回来。

---

## 五、每日流水线做什么

`.github/workflows/daily.yml`：

1. `cron: 37 17 * * *`（每天 17:37 UTC ≈ 北京时间次日 01:37）触发，也可手动 `Run workflow`。
2. 跑 `python scripts/sync_upstream.py`（拉上游，没配订阅就直接跳过）。
3. 跑 `python scripts/autobuild.py`（三闸门深度检测 + 生成）。
4. 把 `state/`、`dist/`、`config/` 的变化 commit 回仓库（带当天日报）。
5. 把 `dist/` 发布到 GitHub Pages → 你就有了稳定的 `tvbox.json` 地址。
6. 日报同时写进 Actions 的 **Summary**。

想要更高频？把 `cron` 改成每 6 小时一次即可（同一 repo 的 schedule 任务最少间隔 5 分钟，
但 GitHub 对活跃度低的仓库会自动降频，必要时用 `workflow_dispatch` 手动补跑）。

---

## 六、关于 Cloudflare

你也可以用 **Cloudflare Workers + Cron Triggers** 替代第 3、4 步：

- Worker 每天跑同样的 Python（Workers 用 `pyodide` 或改写成 JS），把结果写进 **KV**；
- 对外暴露 `https://<你的域名>/tvbox.json`，还可以顺带做缓存、UA 伪装、按地区分流。

好处是国内访问通常比 `raw.githubusercontent.com` 稳，坏处是多一套部署。
本仓库默认走 **GitHub Actions + Pages**（零额外部署），需要时再接 CF 即可。

---

## 七、常见问题

**Q：我一个源都没有，输出里全是 ❌？**
`config/sources.json` 里默认放的是**结构模板**（`*.example.com`）和两条**合法的公开 HLS 测试流**，
用来验证流水线本身是通的。用 `import_sources.py` 导入你自己的源，或手工替换掉模板条目。

**Q：为什么某些源第一天就被删了？**
不会。默认 `drop_after = 3`，第一次失败只进 `grace`。除非你的 `settings` 被改小了。

**Q：`csp_Xxx` 那种站为什么一直显示「无法验证」？**
它的可执行逻辑在公共 `jar` 里，没有独立 URL 可探。可以给它加一个 `probe` 指到站点首页或采集接口。

**Q：APK 里填了地址但不生效？**
先用浏览器打开你的 `tvbox.json` 确认能返回 JSON；再确认地址是 **https**、没有多余空格；
FongMi/TVBox 有些版本需要先在「设置 → 接口」里点「确认/保存」才会重载。

**Q：想强制立刻重检？**
Actions → `Daily source check` → `Run workflow` → 勾选 `force`。

---

## 八、免责声明

本仓库只是一个**通用的 URL 健康检查与配置生成工具**，本身不包含、不提供、不代理任何影视内容。
`config/sources.json` 中的条目需由使用者自行提供并自行承担合规责任。
请确保你所采集、聚合与分发的内容在你所在地区拥有合法授权，勿用于任何商业用途。

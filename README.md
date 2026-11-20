# ai-oss-radar

自动发现 GitHub 上**重要、有影响力**的 AI 开源项目，产出两份榜单：**影响力榜**和**增长势头榜**。

```bash
export GITHUB_TOKEN=ghp_xxx          # 经典 token 即可，只需 public_repo 读权限
python3 -m aiscout scan              # 完整扫描，产出 reports/latest.md
```

---

## 为什么不能只看 star

star 数是个**滞后、易刷、赢家通吃**的指标。只按 star 排序，你会得到一份三年前就正确的榜单，外加一堆 awesome-list。

所以这个工具保留 star，但做对数压缩、权重压到约四分之一，其余权重分给更难伪造的信号：

| 信号 | 说明 | 为什么有用 |
|---|---|---|
| contributors | 贡献者数 | 刷不出来。一个人的项目和一个社区，差别就在这 |
| forks | fork 数 | fork 是"我要在上面干活"，star 只是"我收藏一下" |
| push 新鲜度 | 距最后一次 push 的天数，指数衰减，半衰期 30 天 | 区分活着的项目和遗迹 |
| star 增量 | 最近真实的周增长（来自本地快照） | 这才是"势头"，生涯平均不是 |
| license / releases | 有无许可证、有无发版 | 决定别人能不能真的依赖它 |

所有信号先转成**在候选池内的百分位**再加权。这样不用手调魔法常数，也能扛住 GitHub 上每个计数都呈幂律分布的事实。

**"有影响力"和"正在爆发"是两个不同的问题**，所以输出两份榜单，而不是混成一个两头不靠的分数。

---

## 三级流水线

```
① 发现（便宜，覆盖广）     搜索 API，约 50–400 次请求  →  几千个候选
② 富化（昂贵，只做前列）   每仓库约 3 次请求           →  前 250 名
③ 打分 + 快照 + 出报告     纯本地计算
```

### ① 发现 — 绕开 1000 条上限

搜索 API 对任何查询**最多只返回 1000 条**，`total_count` 再大也没用。所以每个查询按 star 区间递归切分，直到每片都在上限以下：

```
topic:llm stars:>=300        →  4200 条，超了
├── stars:300..1700          →   900 条  ✓ 取全部
└── stars:1701..1000000      →  3300 条，还超
    ├── stars:1701..9000     →   800 条  ✓
    └── stars:9001..1000000  →   ...
```

切分点取**几何中点**（`sqrt(lo*hi)`）而不是算术中点——star 分布是幂律的，算术中点会把几乎所有仓库都塞进低区间，白切一刀。

一次搜索请求返回 100 条**完整**仓库记录（star / fork / topics / pushed_at / license 全都有），所以这一级几十次请求就能覆盖几千个仓库。

**每个分片只发一次探测请求。** `total_count` 本来就在结果响应里，所以单独发一次请求去问总数，等于为一个马上就要送到的数字多花一次配额。而且绝大多数分片装不满一页——对它们来说那一次计数请求就是**一半的成本**。探测拿回来的那一页数据一定会被收下，不管这个分片是整个取走还是继续细分。

这也是为什么顶层分片有一个 `STAR_CEILING` 上界而不是开区间：开区间得先多探一次才知道天花板在哪。

覆盖面来自三路并行：

- **核心 topics**（`llm` `ai-agents` `rag` `mcp` `diffusion-models` …，约 60 个）—— 精确度高
- **邻域 topics**（`machine-learning` `pytorch` `cuda` …）—— 范围大，所以用更高的 star 门槛
- **关键词探针** —— 搜 name/description/readme，捞那些**压根没打 topic** 的重要项目

### ② 富化 — 真正区分项目质量的两个数

搜索结果告诉你一个仓库有多红，但不告诉你**除作者之外还有没有人在干活**，以及**上个月它还活着吗**。这两件事需要单仓库端点：

```
contributors  /repos/{o}/{r}/contributors?per_page=1
              → 不翻页，直接读 Link rel="last" 里的页码当人数。一次请求搞定
commits_90d   /repos/{o}/{r}/stats/commit_activity
              → 一次请求拿 52 周的周提交数。GitHub 冷缓存时返回 202，客户端会重试
releases      /repos/{o}/{r}/releases?per_page=1
              → 发版节奏
```

每仓库约 3 次请求，core 配额 5000/小时，所以这一级只跑前 N 名（默认 250）。

### ③ 打分

**影响力榜**（看长期分量）

```
0.26 stars(log)  0.18 贡献者  0.16 活跃度  0.14 势头
0.11 forks       0.08 成熟度  0.07 存续时长
```

**势头榜**（看当下爆发）

```
0.40 周绝对增长  0.25 增长占总 star 比  0.15 加速度
0.12 活跃度      0.08 贡献者
```

相关度不作为加权项，而是**作为闸门**（`gate = relevance / 0.6`）——否则一个勉强沾 AI 的仓库能靠 star 数硬挤进来。

---

## star 历史：GitHub 不给，只能自己攒

GitHub **不提供** star 历史 API。`/stargazers` 带 `starred_at` 可以翻，但**翻到 40 000 条就停**，所以最大的那些仓库根本没法这么重建。

唯一可行的办法是**每次运行都存快照，然后和过去比**：

- 第 1 次运行：没有历史，退化成生涯平均（`stars / age`）。这会**高估新项目、低估老项目**，报告里会明确标注 `momentum source: lifetime`
- 第 2 次之后：用真实的周增量，`momentum_src` 变成 `delta/7d`

`baselines()` 会挑**最接近目标天数**的那个快照，并且拒绝区间外的——一个 2 天前的快照不会被冒充成 7 天基线。所以哪怕跑漏了几天，算出来的周增长率依然是对的。

**要攒历史，就得定时跑。** 见下面的 GitHub Actions。

---

## 诚实标记

分数已经把这些折算进去了，但还是列出来让你自己判断：

| 标记 | 触发条件 | 含义 |
|---|---|---|
| `suspect-stars` | star > 5000 且 fork/star < 1% | 真实项目几乎不会这样。买星，或者纯书签仓库 |
| `solo-project` | star > 3000 但贡献者 ≤ 2 | 不一定是坏事，但谈不上"社区影响力" |
| `dormant` | 一年多没 push | |
| `too-new` | 建仓不到 21 天 | 还看不出来 |
| `no-license` | 没有许可证 | 法律上你不能用 |
| `not-software` | awesome-list / 教程 / 路线图 | 单独归类，默认不进主榜 |
| `weak-ai-signal` | 相关度 < 0.35 | 大概是被关键词误捞的 |

`suspect-stars` 的 1% 阈值有实测依据：langchain 约 0.10，pytorch 约 0.27，正常开源项目在 0.05–0.3，买星的通常低于 0.01。

---

## 常用命令

```bash
# 完整扫描
python3 -m aiscout scan

# 快速扫描：只跑核心 topics，跳过邻域和关键词
python3 -m aiscout scan --quick

# 只跑指定 topic
python3 -m aiscout scan --topics llm,rag,mcp --min-stars 1000

# 只做一级，完全不碰单仓库端点
python3 -m aiscout scan --no-enrich

# 把 awesome-list / 教程也算进榜
python3 -m aiscout scan --include-lists

# 不限制同一 owner 的仓库数（默认每个 owner 最多 3 个）
python3 -m aiscout scan --max-per-owner 0

# 看某个仓库在本地快照里的历史
python3 -m aiscout trend langchain-ai/langchain

# 查 API 配额
python3 -m aiscout limits
```

输出：

```
reports/latest.md        给人看的 Markdown 榜单
reports/latest.json      给程序用的完整数据，含每个信号的分项得分
data/aiscout.db          SQLite 快照库 —— 这个别删，势头全靠它
```

---

## 定时运行与网页

`.github/workflows/radar.yml` 每天跑一次，做四件事：

1. 扫描 GitHub，重新打分
2. 把报告和快照库提交回仓库
3. 生成 `site/` 下的网页数据
4. 把 `site/` 发布到 GitHub Pages

所以**网页是自己更新的**，不需要任何人去刷新。想立刻更新就去 Actions 点一下 Run workflow，几分钟后网页就是新的。

### 只需要做一次的设置

1. 仓库 **Settings** → 左侧 **Pages** → Source 选 **GitHub Actions**
2. 仓库 **Actions** 页 → 左侧 **AI OSS radar** → 右侧 **Run workflow**

之后每天早上自动跑。扫描用的是 Actions 自带的 `GITHUB_TOKEN`，不用配任何密钥。

**攒够两次运行，势头榜才有意义**（原因见上面那节）。第一次运行后网页上那条黄色提示会说明这一点，等真实周环比出来之后提示会自动消失，标签名也会从"增长榜"变回"势头榜"。

### 网页本身

`site/index.html` 是**零依赖**的单文件页面：不加载任何 CDN、不用外部字体，只 `fetch` 同目录下的两个 JSON。这样它在网络受限的环境里也能正常打开，也不会因为某个 CDN 改版而哪天突然坏掉。

有搜索框、分类筛选、点列头排序、图例点击只看某一类、深浅配色切换（记在 `localStorage`），以及一张双对数散点图——横轴 star 总数、纵轴周增速，一眼能看出"老牌"和"新来的"分布在哪。

本地预览：

```bash
python3 scripts/flatten.py reports/latest.json site/radar-rows.json \
    --run-out site/radar-run.json
python3 -m http.server -d site 8000      # 然后开 http://localhost:8000
```

### 可选：加个 PAT 把富化范围调大

自带的 `GITHUB_TOKEN` 的 core 配额是 1000 次/小时，每个仓库富化要约 3 次请求，所以 workflow 在没有 PAT 时只富化前 120 名。个人 PAT 是 5000 次/小时，能富化前 250 名。

搜索配额是 30 次/分钟，和 token 类型无关，所以**一级发现阶段不受影响**，两种情况下覆盖面一样。

要加的话：

1. GitHub 右上头像 → **Settings**（账号设置，不是仓库设置）
2. 左侧拉到最底 → **Developer settings**
3. **Personal access tokens** → **Tokens (classic)** → **Generate new token (classic)**
4. Note 随便写（比如 `radar`），Expiration 选 90 天或 No expiration
5. 勾选范围：只勾 **`public_repo`** 一个就够
6. 生成后**立刻复制**那串 `ghp_...`，页面关掉就看不到了
7. 回到这个仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
8. Name 填 `RADAR_TOKEN`，Secret 粘贴刚复制的那串 → Add secret

下次运行就会自动用它，日志开头会写明用的是哪个。

---

## 已知限制

- **"影响力"没有客观真值。** 权重是判断，不是测量。想调就改 `config.py` 里的 `WEIGHTS_INFLUENCE`
- **依赖数（dependents）是最强的影响力信号，但 REST API 不提供。** 只能爬 `/network/dependents` HTML 页面，很脆，所以没做。真要加的话，建议改用各语言包管理器的下载量（PyPI / npm）
- **贡献者数在 500 封顶。** GitHub 只解析前 500 个作者邮箱，超出的算匿名。所以 `transformers`（实际几千贡献者）只报 439。超过 `CONTRIBUTOR_SATURATION`（默认 400）的一律并列，避免排名被偶然因素左右——但这也意味着这个信号在大项目之间不再有区分度
- **英文偏向。** topic 和关键词都是英文的，中文项目若只写中文描述，可能只能靠 topic 捞到
- **第一次运行的势头榜不可信**，原因见上。跑够两次再看

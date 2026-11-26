# NyYuketang

长江雨课堂（changjiang.yuketang.cn）自动化 —— **视频进度 + 目录遍历 + 讨论作业**。

走**接口层**而不是 DOM 点击，所以快到不需要等：102 个视频 3 分钟刷完，103 个讨论区 3 分钟发完。

---

## 实测成绩（本次）

| 项目 | 结果 |
|---|---|
| 课程 | 《某思政课》classroom=CLASSROOM_ID，108 个学习单元 |
| 视频 | 61 → **102/102 完成**，用时 162 秒 |
| 讨论 | **103/103 单元已发帖**（剩 5 个是作业题，非讨论区） |
| 作业 | 10 题卷子 **4/4 补答成功**，已做的 6 题自动跳过，用时 53 秒 |
| 学习讨论 | 班级讨论区 6 条，AI 现写评论，单条约 600 字 |

---

## 安装

```powershell
python -m pip install -r requirements.txt
```

浏览器用系统 Edge（`config.toml` 里 `channel = "msedge"`），不用额外下载 chromium。

## 交互界面（推荐）

```powershell
python -m nyykt
```

```
 _   _       _____         _         Ver 1.0.0 by Redmaple
| \ | |_   _|_   _|__  ___| |__      DeepSeek
|  \| | | | | | |/ _ \/ __| '_ \
| |\  | |_| | | |  __/ (__| | | |
|_| \_|\__, | |_|\___|\___|_| |_|
       |___/
────────────────────────────────────────────────────────────
  当前服务器：长江雨课堂
  登录ID：<uid>  <姓名>
  课程数目：0 未完成  /  102 已完成  （共 102）
  讨论数目：5 未完成  /  103 已完成  （共 108）
  作业数目：4 未完成  /  1 已完成  （共 5）
```

按键：**↑↓ 选择，Enter 进入，Esc 返回，Q 退出**。选中行整条高亮，也支持数字 1-9 直选。
终端矮的时候菜单会自动开窗滚动，底部提示会告诉你「还有 上 3 / 下 5 项」。

**三个数字是三件事**：

| 行 | 指的是 |
|---|---|
| 课程数目 | 视频节点（`leaf_type=0`），刷课刷的就是这个 |
| 讨论数目 | 每个单元下面的「说点什么」讨论区 |
| 作业数目 | 要交卷的作业（`leaf_type=6`），AI 助手可代答，提交你自己点 |

### 作用域（选课程）

刷课、完成讨论、学习讨论、作业——都跑在**当前作用域**那门课里。考试不受影响，它自己带 `classroom_id`。

点「作用域 / 切换课程」进选择页：

```
  作用域  刷课 / 讨论 / 学习讨论都在这里选中的课程下进行

    机械制图Ⅲ  ·  2026秋-(07)203T0201  <教师>
  > 某思政课  ·  2026秋-(01)230T0046  <教师>  ← 当前
    高等数学Ⅰ（一）  ·  2026秋-(01)230T0080  <教师>
```

`↑↓` 选、`Enter` 确认、`R` 重拉、`Esc` 返回。

来源是 `learning_list`（我听的）+ `teaching_list`（我教的），按 `classroom_id` 去重。

**只存 classroom_id**，课程名是运行时带出来的，不落盘冗余字段。

**缓存**：统计状态时顺带拉一份课程列表存进 `state.json`，所以点进去是**立刻出列表**（实测 1ms），后台再静默刷新。

换到一门没内容的课（老师没发布章节）时，统计会清零并把 `empty_course` 标出来，不会留着上一门课的旧数字。

### 讨论数目统计

统计讨论要**逐单元发请求**（一百多个 leaf），比较慢，所以默认关：

```
  讨论数目：未统计（主页可开）
```

主页菜单里有 `讨论统计 [关]`，回车切到「开」会**立刻统计一次**，之后每次刷新状态都会带上。

切回「关」就不再扫描，省掉那一分多钟。

菜单项：

| 项 | 说明 |
|---|---|
| 辅助答题 | 列可用考试 → 选一门 → 读卷 + AI 给答案（答题提交你自己来） |
| 刷课 · 全部 | 把所有未完成视频推满 |
| 刷课 · 区间 | 输入 `1-10` / `1,3,5` 只做这几项 |
| 完成讨论 · 全部 | 所有讨论区发帖（抄现成的，不走 AI） |
| 完成讨论 · 区间 | 同上，限区间 |
| **学习讨论 · 全部** | 班级讨论区（老师发的大题），AI 现写评论发表 |
| **完成作业 · 全部 / 区间** | 逐题截图 → OCR / 多模态 → AI 作答 → 自动勾选，**已做过的题自动跳过** |
| 防封自动化 [开/关] | 见下 |
| 自动讨论 [开/关] | 开启后刷课会顺手发每节的讨论 |
| **讨论统计 [开/关]** | 是否自动统计讨论数目，**默认关** |
| **作用域 / 切换课程** | 从课程列表选一门，刷课 / 讨论 / 学习讨论都跟着它走 |
| 选择服务器 | 目前只接通长江雨课堂，选其他的会提示暂未支持 |
| 设置 | 分区：防封与讨论 / AI 配置 / 作业与界面 |
| 重新登录 | 清旧 cookie 后重新扫码 |
| 刷新状态 | 重新统计 |

任务执行时菜单位置变成进度区，双进度条：总进度 + 当前视频节流进度，Esc 可中止。
**任务跑完会自动刷新状态**，三个数字当场更新。

### 学习讨论

和视频下面那个「单元讨论」不是一回事：

| | 单元讨论 | 学习讨论 |
|---|---|---|
| 位置 | 每节课视频下的讨论区 | 班级讨论区 `/m/v2/discussion/CLASSROOM_ID` |
| 内容 | 一句话感想 | 老师发的论述大题（通常两问、几百字） |
| 策略 | **抄别人的** | **AI 现写**，禁 Markdown，压掉套话 |

接口：

```
列表  GET /v/discussion/v2/topics/?cid=..&limit=20&ol=1&offset=0&real=true&rank=1
发帖  POST /v/discussion/v2/comment/   （to_user 填 topic 的作者）
```

注意参数是 `cid` 不是 `classroom_id`，少了 `real=true`/`rank=1` 会返回"没有权限"。

**去重**：逐条扫评论找自己的 `user_id`，评过就跳过。

**回避笔记**：策略 `avoid_note`（默认开）跳过 `topic_name` 或开头正文含"笔记"的帖子——那些是学习笔记，不是讨论题。

生成用的 prompt 约束：

- 300–450 字，一到两段，不分点、不加标题
- 禁 Markdown（`**`、`#`、`-`、`1.`、`` ` ``）
- 禁套话：「不仅仅是……更是」「在当今社会」「随着……的发展」「让我们一起」「总而言之」「深刻认识到」
- 禁「首先/其次/最后」三段式
- 要求句子长短不齐、可以带一点犹豫，结尾不升华

生成后再过一遍 `nyykt/words.py` 的 117 词词表（商业黑话 / 空泛拔高 / AI 句式 / 网络用语 / 口号表态），命中就用加严的 prompt 重写一次，取命中更少的那版。

温度固定 0.3，比全局默认更低，避免写得太"日常"。

### 完成作业

作业页面的题干是**字体混淆**的（`xuetangx-com-encrypted-font`），直接读文本是乱码，所以流程是：

1. 逐题截图，多模态模型直接看图；模型不支持看图就退回 Windows OCR
2. AI 输出选项字母，程序勾选后**校验每个 `checked`**，不生效就补点
3. **已经做过的题跳过**——读题时先看 `input.checked` 和 `label.is-checked`，有值就不动它，免得把之前选的改掉
4. 答完停在确认页，`homework.auto_submit` 关着就等你选「立即提交」

提交必须点「帮我提交」或「立即提交」，**「先不用了」不计成绩**。


### 走过的弯路（已绕过）

一开始想直接从接口拿题目，试过这些死路：

- `/ai-workspace/lms-graph-mobile/{cid}/homework/{leaf_id}` 路由没挂载，SPA 白屏（HTML 有 978KB，全是图标 symbol）
- `/api/v3/lesson/exercise/homework/{id}/` → 401
- `/v2/api/web/homework/{id}/` 只回一个内部 id，没有题目
- 课程首页 `/CLASSROOM_ID` 同样白屏

后来从封面页点「开始作答」才进到真正的作答页 `/h5/homework/...`（在 iframe 里）。
接口 `/mooc-api/v1/lms/exercise/get_exercise_list/{exercise_id}/{sku}/?classroom_id=`
能拿到题目，但题干是加密字体、`show_answer: 0` 不给答案——所以最终走了截图 + OCR 这条路。

---

## AI 作业作答（截图 → OCR → AI → 勾选）

题干被 `xuetangx-com-encrypted-font` 做了**对抗性字形混淆**：接口给的是乱码，字体里
`uni4E00`（该是「一」）有 8 个轮廓 91 个点。试过 5 种参考字体 × 3 个字重做位图/轮廓比对，
最高相似度 0.44，不收敛——本地解码不通。

但**页面渲染是正确的**。所以换一条路：

```
封面页 → 点「开始作答」→ 展开答题卡跳题
       → 截 .container-problem 的图
       → Windows 自带 OCR（zh-Hans-CN）读回文字
       → AI 解题
       → 点 input[value="A"] 勾选
```

实测把「第二次作业」7 道题全解开了：

```
Q1 [多选] A,B,C,D   以下关于理想的表述中正确的有（）
Q2 [单选] B         理想的（），不仅体现为它受时代条件的制约…
Q3 [多选] A,C       理想转化为现实的基本原则有（）
Q4 [单选] B         关于个人理想与社会理想的关系…错误的是
Q5 [多选] A,B,C,D   关于为什么要信仰马克思主义…
Q6 [单选] B         关于共产主义社会的特征…错误的是
Q7 [单选] B         （）是改革开放以来党的全部理论和实践的主题…
```

### 配置

设置 → AI 作答：

| 项 | 说明 |
|---|---|
| AI 作答 | 开 / 关 |
| AI 服务商 | deepseek / openai / anthropic / custom（切换时自动填 base_url 和模型） |
| AI 模型 | 如 `deepseek-chat`、`gpt-4o-mini`、`claude-3-5-sonnet-latest` |
| API Key | 按 Enter 输入，**只写进 `state.json`，不进代码**（`state.json` 已 gitignore） |
| 测试连接 | 按 Enter 发一次 ping |

协议支持 OpenAI 兼容（DeepSeek / OpenAI）和 Anthropic 原生 `/v1/messages` 两种。

### 关键细节（踩了很久才搞明白）

**切题时会弹「刚刚答完的习题还没有提交，需要提交吗？」**

- 点**「帮我提交」** → 这题的答案计入成绩，然后切到下一题
- 点「先不用了」 → **答案直接被丢掉**，`my_count` 保持 0，成绩里不算

所以逐题切换的过程本身就是逐题提交。7 题答完，整份作业的 `progress` 就到 1 了，
底部按钮也会变成「已提交」——最后不需要再单独点一次「提交」。

**多选题必须逐项校验**

`input.click()` 对 Vue 的 checkbox 有一半不生效，点 `label` 时还可能把前一个取消掉。
现在每勾一项都读 `checked`，勾不上就换三种方式重试（点 label → 点 input → JS 派发
pointerdown/mousedown/mouseup/click）。日志里会打 `clicked=N/N`，对不上会警告。

### 用法

菜单 →「完成作业 · 全部」或「完成作业 · 区间」(`1-3` / `2,4`)。

答完会停住，弹出提交选单：

```
  答案提交  作业 HOMEWORK_ID · 共 7 题

  > 立即提交  (7 题已答)
    查看作答情况
    取消任务（不提交）
```

- **全是客观题** → 「立即提交」可用
- **一旦有主观题**（填空 / 简答 / 论述 / 问答 / 分析 / 名词解释…）→ 变成红色
  `立即提交（第 5 题是主观题，不允许提交）`，回车会被拒绝

「查看作答情况」把每题答案打到输出区，看完自动回到选单。

答案存 `out/hw-<leaf_id>-answers.json`，截图在 `out/hw-<leaf_id>/`。

### 自动提交

设置 → `作业自动提交`，**默认关**。

开了就不再问，答完直接提交；但如果含主观题，依然会跳过提交（不拿半吊子答案去交）。

### 显示浏览器

设置 → `显示浏览器`，**默认静默**。

| 状态 | 行为 |
|---|---|
| 关（默认） | 无头模式，后台跑，不弹窗口 |
| 开 | 弹出受控浏览器窗口，能看到自动操作过程 |

**开了就别碰鼠标键盘。** 你手动点、滑、关页面，会和自动化抢同一个浏览器，
后果是答错题、漏题、提交内容不是你想要的。只想观察的话放着别动就行。

### 输出翻页

`+` / `-`（或 PgUp/PgDn）翻输出区，标题栏实时显示行段 `[9-16/24]`。
新日志进来不会打断你正在看的历史，翻到底按 `-` 就回到最新。

输出区**默认 8 行**，设置 → `输出行数` 可调 8-30（←→ 加减）。

---

## 辅助答题（考试）

> **只读模式**：工具不会点选项、不会填答案、不会提交。它只把题目读出来、让 AI 给答案，
> 你自己在考试页面里作答。

主菜单第一项 →「辅助答题」，粘贴考试链接：

```
https://changjiang.yuketang.cn/v/index/course/normalcourse/examcover/CLASSROOM_ID/EXAM_ID
```

（也可以只填最后那段考试 ID，教室号从当前课程取）

它会打开考试页、点「继续答题」进去、**把整卷读完**，然后逐题让 AI 答。

### 为什么考试比作业简单

作业页的题干被 `xuetangx-com-encrypted-font` 做了对抗性字形混淆，只能截图 + OCR。
**考试页没有字体混淆**，DOM 里就是明文，直接读文本，准确率 100%。

而且考试是整卷渲染（13 个 `.subject-item` 全在 DOM 里），一次读完全部题目，不用翻页。

### 题型

| 题型 | 处理 |
|---|---|
| 单选题 | 给一个字母 |
| 多选题 | 给多个字母 |
| 判断题 | 给 对 / 错 |
| 填空题 | 多空用 `\|` 分隔，按顺序 |
| 主观题 | 给可直接抄写的完整答案 |
| 投票题 | 说明建议选哪项及理由 |

### 进入流程：工具不代点

**考试辅助不看「显示浏览器」设置，永远可见**——按钮得你自己点，无头模式没法操作。

打开封面页后工具**不点任何按钮**，等你操作：

```
请在弹出的浏览器里点「继续答题」，限时提示里也点继续。我在这等你…
```

- 你在弹窗「当前考试限时…是否继续」里点**继续** → 工具检测到题目加载，开始读卷
- 你**不继续**、关掉弹窗、或者关浏览器 → 检测到后**取消任务，回到主菜单**
- **中途按 Esc** → 立即取消（实测 4 秒内响应），浏览器一并关掉
- 默认最多等 **10 分钟**，超时自动取消

这样不会出现"代为操作"的问题——按钮永远是你点的。

### 富文本 / 公式 / 图片

考试题干常常是富文本，公式和图片走**两条不同的路**：

| 内容 | 处理 |
|---|---|
| 普通文字 | `innerText` |
| 表格 | 逐行转 `a \| b \| c` |
| **数学公式** | **从 HTML 重建 LaTeX**，不用多模态 |
| **图片** | **截图喂多模态模型**；模型不支持才退回 OCR |

#### 公式：从 MathJax 还原 LaTeX

雨课堂用 MathJax 渲染公式，外层是 `<span class="kfformula-latex">`，
**原始 LaTeX 源码就藏在里面的 `<script>` 标签里**：

```html
<span class="kfformula-latex">
  <span class="MathJax">…一大坨渲染后的 span…</span>
  <script>\int ^{\frac {\pi } {2}}_{0} {\frac {sinx} {sinx+cox}}dx</script>
</span>
```

所以读题时把每个 `.kfformula-latex` 整个换成 `$<script里的内容>$`，
`innerText` 拿到的 `∫ π 2 0 sinx sinx+cox dx` 那种被拍平的乱码就还原了：

```
原始 innerText（拍平）: 尝试计算 ∫ π 2 0 sinx sinx+cox dx 的定积分
重建之后            : 尝试计算 $\int ^{\frac {\pi } {2}}_{0} {\frac {sinx} {sinx+cox}}dx$ 的定积分
```

上下限、分式全回来了，模型能准确理解。

#### 图片：多模态优先

题目里有 `<img>` 时，截图整道题，base64 编码后直接附在消息里给模型看
（OpenAI 用 `image_url` + `data:image/png;base64,`，Anthropic 用 `image` + `source.base64`）。

**只有模型不支持看图时才退回 OCR**，prompt 里会注明"OCR 可能有错字"。

### 看图能力检测

设置 → `看图能力`，三档：

| 值 | 行为 |
|---|---|
| `auto`（默认） | 按模型名判断（`gpt-4o`/`claude-3`/`qwen-vl`/`gemini`… 算支持） |
| `on` | 强制认为能看图 |
| `off` | 强制认为不能 |

**模型不支持看图时，主页会挂警告**：

```
  ⚠ 当前 AI 模型不支持看图，考试里含图片的题会退回 OCR
```

比如 DeepSeek 只有文本模型，就一直是这条警告。换 `gpt-4o` 或 `claude-3-5-sonnet` 后警告消失，图片自动走多模态。

### 界面

**答题过程中就直接显示题目列表**，不单独走进度条页——边答边看，随时能切题、跳过：

```
  辅助答题  考试 EXAM_ID  1/2 完成  思考中 1
     1. 主观题  ln|x| + C        OK
  >  2. 主观题                   思考中
     3. 判断题                   已跳过
  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
  题目 尝试计算 $\int ^{\frac {\pi } {2}}_{0}…$ 的定积分
  答案 [4-9/9] 设 I = ∫_0^{π/2} sinx/(sinx+cosx) dx。令 x = π/2 - t，则 dx = -dt…
        cos t，cosx = sin t，得 I = ∫_0^{π/2} cos t/(cos t + sin t) dt…
  思考 利用区间对称换元 x=π/2-t，把原积分转化为互补形式…
```

标题行实时显示 `已完成/总数` 和 `思考中 N`、`跳过 N`、`失败 N` 计数。

**答案区自动折行**（按显示宽度算，中文占 2 格）。题干、选项、答案、思考**拼成一整块**，
共 10 行一页，超出整体翻页——不是各翻各的。

标题上的 `[1-10/30]` 是「当前范围/总行数」，`[` / `]` 翻页，每页 10 行。
**默认停在第 1 页**，从题目开始读；切换题目也会重置回第 1 页。

> 中文输入法下 `【` `】` 同样有效，不用切回英文。`+/-` 的全角 `＋＝` / `－－` 也认。

### 按键

| 键 | 动作 |
|---|---|
| ↑↓ | 选题（列表靠上下索引，不分页） |
| Enter | 对当前题**重新思考**（覆盖旧答案） |
| S | **跳过**当前题（标记「已跳过」，之后可 Enter 重答） |
| **+ / -** | **翻输出区**（所有页面通用，包括考试页） |
| **C** | **复制当前题答案**到剪贴板（`Shift+C` 连思路一起复制） |
| **`[` / `]`** | **翻答案区**（整体翻页，每页 10 行，标题上 `[1-10/30]` 是范围） |
| Esc | 返回主菜单（任务还在跑会一并取消并关浏览器） |

状态标记：`OK` 已出答案 / `思考中` / `已跳过` / `失败` / `待答`。

### 复制答案

选中题目按 **C** 直接把答案塞进剪贴板，切到考试页面 `Ctrl+V` 就填上了。

- 小写 `c`：只复制答案
- 大写 `C`（`Shift+C`）：答案 + `【思路】` 一起复制，主观题想看推导过程时用

走的是 Win32 剪贴板 `CF_UNICODETEXT`，中文和 `π` `∫` `√` 这些符号都不会乱码。实测往返一致。

切换题目时答案区自动回到第一页。

### 浏览器不会自己关

答完题**浏览器保持打开**，你在里面照着答案抄，抄完自己关窗口。

Playwright 对象有线程亲和，所以那个线程会一直留着守 Session。下次启动辅助答题时会
**先自动收掉上一次的**，不会卡在 `profile` 被占用。

如果上次的浏览器是你手动关掉的，下次启动会自动跳过清理，不会卡住。

### 答题时带考试标题

prompt 第一行是考试名，让模型知道这是哪门考试、大概什么背景：

```
【原神数学考试】
题型：主观题
题干：尝试计算 $\int ^{\frac {\pi } {2}}_{0} {\frac {sinx} {sinx+cox}}dx$ 的定积分
```

### 已知限制

**考试列表还没打通**。`/v/exam/list`、`/v/exam/exam_list` 等全是 404，
章节接口里能看到考试节点（`leaf_type=5`，带名字和开始时间），但节点里只有
`leaf_id`，**没有 `exam_id`**，两者之间的映射没找到。

所以现在只能粘贴考试链接进。找到 leaf → exam 的映射就能补「选择可用考试」。

取消时的"回到选考试选单"目前退化成回主菜单。

### 菜单分页

主菜单**两列排版**，14 项压成 7 行：

```
  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ 1/2 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
   > 刷课 · 全部            刷课 · 区间
     完成讨论 · 全部        完成讨论 · 区间
     完成作业 · 全部        完成作业 · 区间
     防封自动化  [关]       自动讨论  [开]
     切换课程               选择服务器
     设置                   重新登录
```

方向键四向走：**↑↓ 跨一行（同列上下），←→ 换列**。

放不下时自动开窗滚动，当前页号显示在**分隔线中间**（`1/2`），
底部提示同时告诉你上下还各有多少项（`还有 下 2 项`）。

### 终端矮的时候

`budget_h < 40`（常见 30 行窗口）会自动切换紧凑布局：

- 字符画从 6 行压成 4 行
- 去掉 banner 和状态区之间的分隔线
- 「当前服务器」和「登录ID」合成一行

省下来的行全部让给菜单和输出区，30 行窗口能稳定拿到 **6 行菜单 + 8 行输出**。
窗口拉高就自动恢复完整版。

### 注意

- 作答页在 iframe 里，**直接访问 `/h5/homework/...` 会触发腾讯滑块**；必须走封面页点「开始作答」
- OCR 对中文准确率不错，但会丢标点和个别字；AI 靠语义补，能选对，但不保证 100%
- 碰到主观题（填空/简答）目前只能给出文字答案，还没做自动填写

### 防封自动化

默认**关**（瞬间推完）。开启后按"理论最快播放时间"节流推送：

```
理论耗时 = 视频时长 / 倍速 + 人手点击间隔
```

默认 2x 倍速 + 2 秒点击间隔，即一个 9 分钟的视频铺开成约 4.5 分钟慢慢上报，
每 `节流粒度`（默认 12 秒）发一批心跳，中间真实 sleep。

设置里可调：

| 项 | 可选值 |
|---|---|
| 播放倍速 | 1.0 / 1.25 / 1.5 / 2.0 / 2.5 / 3.0 |
| 人手点击间隔 | 0.5 / 1.0 / 1.5 / 2.0 / 3.0 / 5.0 秒 |
| 节流粒度 | 6 / 10 / 12 / 15 / 20 / 30 秒 |
| 自动讨论 | 开 / 关 |
| 讨论策略 | 混合 / 只抄他人 / 只用大纲 |

设置存在 `state.json`，下次自动记住。

**未登录时**只显示四项：登录 / 选择服务器 / 设置 / 退出。

## 命令行（等价用法）

```powershell
python -m nyykt login
python -m nyykt whoami
python -m nyykt boost   --classroom CLASSROOM_ID [--limit N] [--dry-run]
python -m nyykt discuss --classroom CLASSROOM_ID [--limit N] [--dry-run] [--force]
python -m nyykt report  --classroom CLASSROOM_ID
python -m nyykt tui
```

## 登录（只需一次）

```powershell
python -m nyykt login
```

弹出浏览器 → 扫码 → 自动检测到登录并保存到 `.profile/`。

> 检测依据不是 cookie 名，而是 `GET /api/v3/user/basic-info` 返回的 `code`。
> `csrftoken` 匿名访问也会生成，别被骗。

确认状态：

```powershell
python -m nyykt whoami
```

---

## 原理

### 视频：心跳接口

```
POST /video-log/heartbeat/
{"heart_data":[{"i":5,"et":"playing","cp":120,"fp":100,"tp":120,"d":20,
                "ts":"1794821203836","u":<uid>,"c":<cid>,"v":<leaf_id>,
                "skuid":..,"classroomid":..,"cc":<ccid>,"pg":"<leaf>_<rand>",
                "sq":1,"t":"video",...}]}
```

服务端逻辑（实测）：

- `watch_length += Σ(tp - fp)`，封顶在 `video_length`
- `watch_length >= video_length` → `completed = 1`
- **`cp / tp / ts` 完全是客户端说了算**，服务端不校验真实时间流逝
- 单次请求的增量会被吞一部分（发 330 只加 272），所以默认发两遍轨迹
- `"ended"` 不是合法事件（返回 `Partial data anomaly`），只用 `loadstart / loadeddata / playing`

一次上报就能把一个 9 分钟的视频标成看完，不需要真的播。

### 讨论：评论接口

```
POST /v/discussion/v2/comment/
X-CSRFToken: <cookie 里的 csrftoken>
{"to_user":<unit.user_id>,"topic_id":<topic.id>,
 "content":{"text":"…","upload_images":[]}}
```

- `topic_id` 来自 `GET /v/discussion/v2/unit/discussion/?classroom_id=&leaf_id=&topic_type=0&channel=xt&sku_id=`
- 读评论：`GET /v/discussion/v2/comment/list/{topic_id}/?offset=0&limit=20`（时间倒序）
- **POST 必须带 `X-CSRFToken`**，否则 403 `CSRF Failed`

### 课程目录

```
GET /c27/online_courseware/schedule/course/progress/?classroom_id=<id>
```
一次拿到「章节 → 小节 → 学习单元」整棵树，外加每个单元的 `progress`（1.0 = 完成）。

URL 映射：`/ai-workspace/lms-graph-mobile/{classroom_id}/video/{leaf.id}?node_id={section.id}`
（章节里 `section_leaf_list[].id` 就是 `node_id`）

---

## 踩过的坑（都已处理）

| 现象 | 原因 | 处理 |
|---|---|---|
| 登录"成功"但接口 401 | `csrftoken` 匿名也有 | 改看 `basic-info` 的 `code` |
| 二维码显示不全 | 用了手机视口 430×932 | 登录单独走桌面 1600×1000 |
| 一大批视频"拿不到视频时长" | 新上传的视频 `media.duration = 0` | 先发一条预热心跳，服务端探测后再查 |
| 心跳 200 但 `completed` 还是 0 | `watch_length < video_length` | 按 `video_length` 补满，最多 4 轮 |
| `"ended"` 报 Partial data anomaly | 不是合法事件类型 | 只用 `playing` 等 |
| 讨论 403 CSRF Failed | Django 要 header | 从 cookie 取 `csrftoken` 回填 `X-CSRFToken` |
| 明明发过却判"没发" | `user_comment_num` 不可信 | 拉评论列表比对 `user_id` |

**三个不同的 user id**，别搞混：

| 用途 | 来源字段 |
|---|---|
| 讨论系统 | `basic-info.data.id` |
| 视频心跳 | `leaf_info.data.user_id` / 心跳里的 `u` |
| 讨论区 owner | `unit_discussion.data.user_id`（`to_user` 用这个） |

三个值互不相同，别拿一个当另一个用。

---

## 命令一览

```
python -m nyykt login      [--timeout 420] [--check-url URL]
python -m nyykt whoami
python -m nyykt boost      --classroom ID [--limit N] [--dry-run] [--max-rounds 4]
python -m nyykt discuss    --classroom ID [--limit N] [--dry-run] [--force] [--include-quiz]
python -m nyykt report     --classroom ID [--skip-discussion]
python -m nyykt probe      URL            # dump 页面结构（改版时用）
python -m nyykt explore    URL --out plan.json
python -m nyykt run        --plan plan.json [--only video discussion]
```

DOM 兜底链路（`probe / explore / run`）保留着，接口变了可以退回页面操作。

## 配置

`config.toml`：

```toml
[browser]
channel = "msedge"      # 或 "chrome" / ""（自带 chromium）
profile_dir = ".profile"

[video]
finish_ratio = 0.99
max_rounds = 4          # 在 --max-rounds 里也可覆盖

[discussion]
min_len = 15            # 抄来的讨论至少多少字
max_len = 220
avoid_keywords = ["老师", "助教", "官方", "置顶", "公告"]
fallback_outline = true # 没人发言时抄讲稿
```

## 文件结构

```
nyykt/
  app.py        TUI 主应用（状态机、菜单、任务线程、进度）
  term.py       终端底层（VT、清屏、按键、中文宽度）
  ui.py         界面组件（Banner、状态面板、选中行、进度条）
  state.py      TUI 设置持久化（state.json）

  api.py        接口客户端（CSRF、cookie、统一返回）
  booster.py    视频心跳引擎（轨迹构造、预热、节流模式、验证）
  forum.py      单元讨论（抄帖、大纲兜底、发布、去重）
  classdisc.py  班级讨论区（学习讨论，AI 现写 + 词表过滤）
  exam.py       考试辅助（读卷、LaTeX 还原、逐题作答）
  exams.py      可用考试列表
  homework.py   作业作答（截图 → OCR/多模态 → 勾选 → 提交）
  courses.py    课程列表（作用域）
  words.py      AI 味词表（黑话 / 拔高 / 句式 / 网络用语 / 口号）
  ai.py         AI 客户端（OpenAI / Anthropic、看图、温度、附加提示词）
  ocr.py        Windows OCR（winsdk）
  protect.py    窗口防监视（控制台设不了，仅保留）
  browser.py    持久化 profile + 反后台挂起

  cli.py        命令行入口（全部功能等价）
  video.py      DOM 兜底：原生 video 接管、反暂停、弹窗清扫
  discussion.py DOM 兜底：输入框定位、候选收集
  plan.py       目录抓取（DOM 路径）
  probe.py      侦察报告
  domsnap.py    页面结构快照
  netlog.py     请求记录
  config.py / utils.py
```

## 边界

- **作业可以代答，但提交要你自己点** —— 程序停在确认页，看到「立即提交」才会计成绩
- 心跳覆盖了「看过」这件事，服务端目前不校验真实播放；但接口一变就得重来
- 防封自动化只是把时间线摊开，不等于绝对安全；求稳就默认关 → 开 → `1.0x` 慢慢来
- 别把同一段文字在同一单元反复发，会被判刷屏
- **「防止监视此窗口」固定在「关」** —— `SetWindowDisplayAffinity` 对控制台窗口一律拒绝（`ERROR_ACCESS_DENIED`），即使 `AllocConsole` 自建、窗口归属本进程也不行。要做只能改成 GUI 窗口渲染，属于换架构，当前版本不做。设置项保留但不可更改，`protect.py` 留着等以后用

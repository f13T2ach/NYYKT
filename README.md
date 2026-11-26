<div align="center">
<h1>NyYuketang</h1>

长江雨课堂自动化，一个终端界面搞定

刷课 · 讨论 · 作业代答 · 考试辅助

<p>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square">
  <img alt="platform: Windows" src="https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square">
  <img alt="interface: TUI" src="https://img.shields.io/badge/interface-TUI-4E9A06?style=flat-square">
  <img alt="license: MIT" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square">
</p>
</div>

## 简介

NyYuketang 走**接口层**而不是 DOM 点击，所以快得不需要等：102 个视频 3 分钟刷完，103 个讨论区 3 分钟发完。

它只做四件事：

- **刷课** —— 心跳接口推进度，不看视频也能满
- **讨论** —— 抄同学现成的发言，或从讲稿大纲取一句
- **作业代答** —— 截图 → OCR / 多模态 → AI 给选项，程序替你勾上
- **考试辅助** —— 读整卷、还原公式、逐题给答案，抄写和提交你自己来

浏览器只用来拿登录态和兜底，绝大部分操作是直接打接口。

## 快速开始

需要 Python 3.11+ 和系统 Edge（不下载 chromium）。

```bash
git clone https://github.com/f13T2ach/NYYKT.git
cd NYYKT
pip install -r requirements.txt
playwright install msedge
```

扫码登录一次，之后 profile 一直有效：

```bash
python -m nyykt login
```

然后进界面：

```bash
python -m nyykt
```

## 界面

```
 _   _       _____         _         Ver 1.0.0 by Redmaple
| \ | |_   _|_   _|__  ___| |__      DeepSeek
|  \| | | | | | |/ _ \/ __| '_ \
| |\  | |_| | | |  __/ (__| | | |
|_| \_|\__, | |_|\___|\___|_| |_|
       |___/
──────────────────────────────────────────────────────────
  长江雨课堂  ·  <uid>  <姓名>
  当前课程：<课程名>
  课程数目：0 未完成  /  102 已完成  （共 102）
  讨论数目：5 未完成  /  103 已完成  （共 108）
  作业数目：4 未完成  /  1 已完成  （共 5）

 > 辅助答题                    刷课 · 全部
   刷课 · 区间                 完成讨论 · 全部
   完成讨论 · 区间             学习讨论 · 全部
   完成作业 · 全部             完成作业 · 区间
   防封自动化 [开]             自动讨论 [开]
──────────────────────────────────────────────────────────
```

`↑↓←→` 选择，`Enter` 确认，`Esc` 返回，`Q` 退出。任务跑起来菜单位置变成进度条，`Esc` 中止。

## 功能

| | |
|---|---|
| **作用域** | 从课程列表选一门，刷课 / 讨论 / 学习讨论都跟着它走 |
| **刷课** | 心跳接口推 `et=heartbeat`，1.8 秒一个视频 |
| **完成讨论** | 单元讨论区，抄他人发言或大纲句子，自动去重 |
| **学习讨论** | 班级讨论区的大题，AI 现写评论，禁 Markdown、过词表 |
| **完成作业** | 逐题截图 + AI 作答 + 自动勾选，已做过的题跳过 |
| **辅助答题** | 列可用考试 → 读卷 → 公式还原 → 逐题给答案 |
| **防封自动化** | 按理论播放时间摊开发送，不用瞬间刷 |
| **设置** | 分区管理：防封与讨论 / AI 配置 / 作业与界面 |

AI 支持 OpenAI 与 Anthropic 两种协议，可换服务商、改地址、拉模型列表、调温度、加附加提示词。不支持的模型会自动退回 Windows OCR。

## 命令行

界面里的每一项都有等价命令：

```bash
python -m nyykt whoami
python -m nyykt boost   --classroom CLASSROOM_ID [--limit N] [--dry-run]
python -m nyykt discuss --classroom CLASSROOM_ID [--limit N] [--dry-run] [--force]
python -m nyykt report  --classroom CLASSROOM_ID
```

## 细节

接口逆向过程、踩过的坑、每个功能的实现细节，都在 [DETAILS.md](DETAILS.md)。

## 边界

- 作业可以代答，但**提交要你自己点** —— 程序停在确认页，看到「立即提交」才会计成绩
- 心跳覆盖了「看过」这件事，但接口一变就得重来
- 别把同一段文字在同一单元反复发，会被判刷屏

---

仅用于个人学习研究的自动化实验。使用前请确认符合所在平台的服务条款。

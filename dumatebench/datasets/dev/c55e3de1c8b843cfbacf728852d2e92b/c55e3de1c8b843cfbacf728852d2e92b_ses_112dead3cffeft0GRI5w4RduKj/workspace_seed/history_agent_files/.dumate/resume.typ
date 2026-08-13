// ── 覃广燕 · 精美简历 ───────────────────────────────
// 左侧藏青色侧边栏 + 右侧白色内容区 · 单页A4

#let accent = rgb("#1B2D4A")    // 藏青主色
#let accent2 = rgb("#2E4A7A")   // 侧边栏模块底色
#let text-dark = rgb("#2D2D2D") // 正文深灰
#let text-muted = rgb("#888888")// 辅助灰
#let white = rgb("#FFFFFF")
#let font-cn = "Source Han Serif CN"
#let font-en = "Liberation Sans"

#set page(
  paper: "a4",
  margin: 0pt,
)

#set text(font: (font-cn, font-en), lang: "zh", fill: text-dark)

// 头像路径
#let photo = "photo.jpeg"

// ════════════════════════════════════════════════════
// 主布局：左右分栏，各占100%高度
// ════════════════════════════════════════════════════
#grid(
  columns: (28%, 72%),
  rows: 100%,
  gutter: 0pt,

  // ━━ 左侧侧边栏 ━━
  [
    #block(fill: accent, width: 100%, height: 100%, inset: (x: 14pt, y: 18pt))[
      // ── 头像 ──
      #align(center)[
        #box(
          width: 60pt, height: 78pt,
          stroke: 2pt + white,
          clip: true
        )[
          #image(photo, width: 100%)
        ]
        #v(8pt)
        #text(font: font-cn, size: 15pt, weight: "bold", fill: white)[覃广燕]
        #v(4pt)
        #text(font: (font-en, font-cn), size: 8.5pt, fill: rgb("#A8B8D0"))[
          幼儿教师 / 钢琴教师
        ]
      ]

      #v(18pt)

      // ── 基本信息 ──
      #text(font: font-cn, size: 9.5pt, weight: "bold", fill: white)[基本信息]
      #v(6pt)
      #line(length: 100%, stroke: 0.5pt + rgb("#3A5A8A"))
      #v(6pt)

      #text(size: 7.8pt, fill: rgb("#C8D4E8"))[
        #set par(spacing: 6pt, leading: 8pt)
        *电话*
        14778305031

        *教育背景*
        国家开放大学
        2017.6 — 2019.7

        *证书资质*
        钢琴十级
      ]

      #v(14pt)

      // ── 专业技能 ──
      #text(font: font-cn, size: 9.5pt, weight: "bold", fill: white)[专业技能]
      #v(6pt)
      #line(length: 100%, stroke: 0.5pt + rgb("#3A5A8A"))
      #v(6pt)

      #text(size: 7.8pt, fill: rgb("#C8D4E8"))[
        #set par(spacing: 5pt, leading: 7.5pt)
        • 五大领域教学
        • 幼儿发展观察
        • 钢琴演奏与教学
        • 家园沟通组织
        • 班级环境创设
        • 教研评课活动
      ]

      #v(14pt)

      // ── 自我评价 ──
      #text(font: font-cn, size: 9.5pt, weight: "bold", fill: white)[自我评价]
      #v(6pt)
      #line(length: 100%, stroke: 0.5pt + rgb("#3A5A8A"))
      #v(6pt)

      #text(size: 7.5pt, fill: rgb("#C8D4E8"), weight: "regular")[
        #set par(spacing: 5pt, leading: 8pt, justify: true)
        热爱幼儿教育事业，具备扎实的专业功底与丰富的实践经验。工作中注重细节，善于发现每个孩子的闪光点，因材施教。性格温和有耐心，沟通能力强。
      ]
    ]
  ],

  // ━━ 右侧内容区 ━━
  [
    #block(width: 100%, height: 100%, inset: (left: 20pt, right: 18pt, top: 18pt, bottom: 14pt))[
      // ── 个人简介 ──
      #text(font: font-cn, size: 12pt, weight: "bold", fill: accent)[个人简介]
      #v(5pt)
      #line(length: 100%, stroke: 1.2pt + accent)
      #v(7pt)
      #text(size: 8.3pt, fill: text-dark)[
        #set par(leading: 9pt, justify: true, spacing: 5pt)
        拥有近6年幼儿园一线教学经验及3年钢琴培训经历，熟悉健康、语言、社会、科学、艺术五大领域教学活动设计与实施。善于根据幼儿年龄特点与个体差异制定个性化教育方案，注重游戏化教学与探究性学习。具备较强的家园沟通能力与班级管理能力，工作细致耐心，富有亲和力。
      ]

      #v(12pt)

      // ── 工作经历 ──
      #text(font: font-cn, size: 12pt, weight: "bold", fill: accent)[工作经历]
      #v(5pt)
      #line(length: 100%, stroke: 1.2pt + accent)
      #v(7pt)

      // ─ 主经历 ─
      #grid(columns: (auto, 1fr), gutter: 6pt)[
        #text(size: 8.5pt, weight: "bold", fill: accent)[高州市永盛幼儿园]
        #align(right)[#text(size: 7.5pt, fill: text-muted)[2019.10 — 2025.8]]
      ]
      #text(size: 8pt, fill: rgb("#4A6FA5"), weight: "regular")[幼儿教师]
      #v(5pt)
      #text(size: 7.8pt, fill: text-dark)[
        #set par(leading: 8.5pt, spacing: 4pt, justify: true)
        *教育教学：*制定班级教学计划，开展健康、语言、社会、科学、艺术五大领域教学活动；运用游戏、互动、户外活动等多元形式激发幼儿兴趣，引导探究性学习；定期观察记录幼儿成长发展情况，分析发展状态并制定个性化教育方案。

        *保育安全：*负责幼儿一日生活照料（用餐、午睡、个人卫生等），配合做好班级卫生消毒与保健工作；严格落实安全制度，开展自我保护意识教育，及时处理突发状况。

        *班级管理：*维护班级日常秩序，管理教具玩具等教学资源；根据教学主题更新教室环境布置，创设兼具安全性与教育意义的学习生活环境。

        *家园共育：*定期与家长沟通幼儿在园表现，提供科学育儿建议；组织家长会、家长开放日、亲子活动等，结合家庭教育环境共同制定适配方案。

        *专业成长：*积极参与教研活动、业务培训、公开课听课评课，持续更新教育理念；配合园所完成活动策划、招生协助及行政记录等工作。
      ]

      #v(7pt)

      // ─ 副经历1 ─
      #grid(columns: (auto, 1fr), gutter: 6pt)[
        #text(size: 8.5pt, weight: "bold", fill: accent)[永盛琴行]
        #align(right)[#text(size: 7.5pt, fill: text-muted)[2020 — 2022]]
      ]
      #text(size: 8pt, fill: rgb("#4A6FA5"), weight: "regular")[钢琴教师]
      #v(3pt)
      #text(size: 7.8pt, fill: text-dark)[
        #set par(leading: 8pt, spacing: 3pt)
        • 负责钢琴培训课程教学，根据学员基础与学习目标制定个性化教学方案
        • 指导学员掌握正确的弹奏技法与乐理知识，跟踪学习进度并定期反馈
      ]

      #v(5pt)

      // ─ 副经历2 ─
      #grid(columns: (auto, 1fr), gutter: 6pt)[
        #text(size: 8.5pt, weight: "bold", fill: accent)[卡尔威琴行]
        #align(right)[#text(size: 7.5pt, fill: text-muted)[2019]]
      ]
      #text(size: 8pt, fill: rgb("#4A6FA5"), weight: "regular")[钢琴教师]
      #v(3pt)
      #text(size: 7.8pt, fill: text-dark)[
        #set par(leading: 8pt, spacing: 3pt)
        • 担任钢琴培训教师，承担日常钢琴教学工作
        • 帮助零基础学员建立正确的弹奏习惯与音乐素养
      ]

      #v(10pt)

      // ── 教育背景 ──
      #text(font: font-cn, size: 12pt, weight: "bold", fill: accent)[教育背景]
      #v(5pt)
      #line(length: 100%, stroke: 1.2pt + accent)
      #v(7pt)

      #grid(columns: (auto, 1fr), gutter: 6pt)[
        #text(size: 8.5pt, weight: "bold", fill: accent)[国家开放大学]
        #align(right)[#text(size: 7.5pt, fill: text-muted)[2017.6 — 2019.7]]
      ]
      #text(size: 8pt, fill: rgb("#4A6FA5"), weight: "regular")[专科]
      #v(4pt)
      #text(size: 7.8pt, fill: text-dark)[
        #set par(leading: 8pt, spacing: 3pt)
        系统学习学前教育相关专业课程，具备扎实的理论基础与实践能力。
      ]
    ]
  ]
)

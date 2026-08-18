/**
 * 字体库：给「改完字想换个字体」用的分组候选表，只列本机真的装了的。
 *
 * 与 fontmatch.js 的分工——fontmatch 负责自动认出原图用的是什么字体，为了速度只跑十来个
 * 最常见的家族（每层要 × 字重 × 逐像素比对）；这里是给人手动挑的，所以铺得很宽，
 * 中西文加起来一百多个，包含腾讯体这类品牌字体。
 *
 * 可用性必须实测，不能照着名单直接列：装不上的字体在 CSS 里会安静地退回系统兜底字体，
 * 用户选了「阿里巴巴普惠体」结果画出来还是苹方，比不给这个选项更糟。
 */

const PROBE_LATIN = 'mmwWiI08gQ';
const PROBE_CJK = '永国字体的书';
const PROBE_MIXED = '永国字体 mmwWiI08';
const MISSING = '__iee_no_such_font__';

const scratch = document.createElement('canvas');
const sigCache = new Map();
const availCache = new Map();

/**
 * 渲染指纹：把探针文字用 `字体, 兜底` 画一遍，取 alpha 通道的哈希。
 * 不能比字宽——中日韩字符在所有中文字体里都是全角方块，宽度恒等于字号，量出来完全一样。
 */
function signature(fam, fallback, probe) {
  const key = `${fam}||${fallback}||${probe}`;
  if (sigCache.has(key)) return sigCache.get(key);
  const size = 44;
  const spec = `400 ${size}px ${fam}, ${fallback}`;
  const ctx0 = scratch.getContext('2d', { willReadFrequently: true });
  ctx0.font = spec;
  const w = Math.min(2000, Math.ceil(ctx0.measureText(probe).width) + 8);
  const h = Math.ceil(size * 1.8);
  scratch.width = w; scratch.height = h;
  const ctx = scratch.getContext('2d', { willReadFrequently: true });
  ctx.clearRect(0, 0, w, h);
  ctx.font = spec;
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = '#000';
  ctx.fillText(probe, 4, Math.round(size * 1.3));
  const px = ctx.getImageData(0, 0, w, h).data;
  let hash = 2166136261;
  for (let i = 3; i < px.length; i += 4) {
    hash ^= px[i] >> 5;
    hash = (hash * 16777619) >>> 0;
  }
  const sig = `${w}x${h}:${hash}`;
  sigCache.set(key, sig);
  return sig;
}

/**
 * 字体在本机是否真的可用。
 *
 * 主判据是「换兜底字体结果不变」：字体装了的话，`字体, monospace` 和 `字体, serif`
 * 都由这个字体来画，两次渲染逐像素相同；没装则分别掉到 monospace 和 serif 上，明显不同。
 * 这一招对中文字体也成立，因为中文字体基本都自带西文字形，混排探针里那串拉丁字母
 * 会跟着中文一起被它画出来。
 *
 * 兜底判据是给少数只有汉字字形的字体（某些书法体、演示体）留的后路：拿纯汉字探针
 * 跟「一个不存在的字体」的渲染结果比，不一样就说明这个家族确实被用上了。
 */
export function fontAvailable(fam, isCJK) {
  const key = `${isCJK ? 'c' : 'l'}|${fam}`;
  if (availCache.has(key)) return availCache.get(key);
  const probe = isCJK ? PROBE_MIXED : PROBE_LATIN;
  let ok = signature(fam, 'monospace', probe) === signature(fam, 'serif', probe);
  if (!ok && isCJK) {
    ok = signature(fam, 'monospace', PROBE_CJK)
      !== signature(MISSING, 'monospace', PROBE_CJK);
  }
  availCache.set(key, ok);
  return ok;
}

/* --------------------------------------------------------------------- */
/* 候选表。fam 只写家族名（探测用），value 追加同族备选与通用兜底（渲染用）      */
/* --------------------------------------------------------------------- */

const CJK_GROUPS = [
  ['品牌字体', 'sans-serif', [
    ['腾讯体', '"腾讯体", "TencentSans", "Tencent Sans"'],
    ['阿里巴巴普惠体', '"Alibaba PuHuiTi", "阿里巴巴普惠体", "Alibaba PuHuiTi 3.0"'],
    ['MiSans 小米', '"MiSans", "MiSans VF"'],
    ['HarmonyOS Sans 鸿蒙', '"HarmonyOS Sans SC", "HarmonyOS Sans"'],
    ['OPPO Sans', '"OPPO Sans", "OPPOSans"'],
    ['抖音美好体', '"DouyinSansBold", "抖音美好体"'],
    ['京东朗正体', '"JDLangZhengTi", "京东朗正体"'],
    ['得意黑', '"Smiley Sans", "得意黑"'],
    ['优设标题黑', '"优设标题黑", "YouSheBiaoTiHei"'],
    ['欣意冠黑体', '"字体圈欣意冠黑体2.0", "字体圈欣意冠黑体"'],
    ['霞鹜文楷', '"LXGW WenKai", "霞鹜文楷"'],
    ['演示悠然小楷', '"演示悠然小楷", "SlideYouRanXiaoKai"'],
  ]],
  ['黑体 · 无衬线', 'sans-serif', [
    ['苹方 PingFang', '"PingFang SC", "苹方-简"'],
    ['冬青黑体', '"Hiragino Sans GB", "冬青黑体简体中文"'],
    ['黑体 Heiti', '"Heiti SC", "黑体-简"'],
    ['华文黑体', '"STHeiti", "华文黑体"'],
    ['兰亭黑', '"Lantinghei SC", "兰亭黑-简"'],
    ['思源黑体', '"Source Han Sans SC", "Noto Sans SC", "思源黑体"'],
    ['方正黑体', '"方正黑体_GBK", "FZHei-B01S"'],
    ['微软雅黑', '"Microsoft YaHei", "微软雅黑"'],
    ['等线 DengXian', '"DengXian", "等线"'],
    ['苹方（繁）', '"PingFang TC", "苹方-繁"'],
    ['苹方（港）', '"PingFang HK", "苹方-港"'],
  ]],
  ['宋体 · 衬线', 'serif', [
    ['宋体 Songti', '"Songti SC", "宋体-简"'],
    ['华文宋体', '"STSong", "华文宋体"'],
    ['思源宋体', '"Source Han Serif SC", "Noto Serif SC", "思源宋体"'],
    ['简宋', '"简宋", "JianSong"'],
    ['华康雅宋', '"华康雅宋体W9(P)", "DFYaSong"'],
    ['儷宋 Pro', '"LiSong Pro", "儷宋 Pro"'],
    ['明朝体', '"Hiragino Mincho ProN"'],
    ['新宋体 / 中易宋体', '"SimSun", "NSimSun", "宋体"'],
  ]],
  ['楷体 · 书法', 'serif', [
    ['楷体 Kaiti', '"Kaiti SC", "楷体-简"'],
    ['华文楷体', '"STKaiti", "华文楷体"'],
    ['行楷', '"Xingkai SC", "行楷-简"'],
    ['隶变', '"Libian SC", "隶变-简"'],
    ['魏碑', '"Weibei SC", "魏碑-简"'],
    ['报隶', '"Baoli SC", "报隶-简"'],
    ['手札体', '"Hannotate SC", "手札体-简"'],
    ['翩翩体', '"HanziPen SC", "翩翩体-简"'],
    ['娃娃体', '"Wawati SC", "娃娃体-简"'],
    ['雅痞', '"Yuppy SC", "雅痞-简"'],
    ['仿宋', '"STFangsong", "华文仿宋", "FangSong"'],
  ]],
  ['圆体 · 其它', 'sans-serif', [
    ['圆体 Yuanti', '"Yuanti SC", "圆体-简"'],
    ['丸黑体', '"Hiragino Maru Gothic ProN"'],
    ['蘋果儷中黑', '"Apple LiGothic", "蘋果儷中黑"'],
  ]],
];

const LATIN_GROUPS = [
  ['无衬线', 'sans-serif', [
    ['Helvetica Neue', '"Helvetica Neue"'],
    ['Helvetica', 'Helvetica'],
    ['Arial', 'Arial'],
    ['Arial Narrow', '"Arial Narrow"'],
    ['SF Pro', '"SF Pro Display", "SF Pro Text", -apple-system'],
    ['Avenir Next', '"Avenir Next"'],
    ['Avenir', 'Avenir'],
    ['Futura', 'Futura'],
    ['Optima', 'Optima'],
    ['Gill Sans', '"Gill Sans", "Gill Sans MT"'],
    ['Verdana', 'Verdana'],
    ['Tahoma', 'Tahoma'],
    ['Trebuchet MS', '"Trebuchet MS"'],
    ['Segoe UI', '"Segoe UI"'],
    ['Roboto', 'Roboto'],
    ['Inter', 'Inter'],
    ['Montserrat', 'Montserrat'],
    ['Open Sans', '"Open Sans"'],
    ['Lato', 'Lato'],
    ['Poppins', 'Poppins'],
  ]],
  ['衬线', 'serif', [
    ['Georgia', 'Georgia'],
    ['Times New Roman', '"Times New Roman"'],
    ['Palatino', 'Palatino, "Palatino Linotype"'],
    ['Baskerville', 'Baskerville'],
    ['Didot', 'Didot'],
    ['Garamond', 'Garamond, "EB Garamond"'],
    ['Hoefler Text', '"Hoefler Text"'],
    ['Rockwell', 'Rockwell'],
    ['American Typewriter', '"American Typewriter"'],
    ['Playfair Display', '"Playfair Display"'],
    ['Merriweather', 'Merriweather'],
  ]],
  ['等宽 · 代码', 'monospace', [
    ['Menlo', 'Menlo'],
    ['Monaco', 'Monaco'],
    ['SF Mono', '"SF Mono"'],
    ['Courier New', '"Courier New"'],
    ['JetBrains Mono', '"JetBrains Mono"'],
    ['Fira Code', '"Fira Code"'],
    ['Consolas', 'Consolas'],
  ]],
  ['标题 · 装饰', 'sans-serif', [
    ['Impact', 'Impact'],
    ['Arial Black', '"Arial Black"'],
    ['Bebas Neue', '"Bebas Neue"'],
    ['Oswald', 'Oswald'],
    ['Copperplate', 'Copperplate'],
    ['Chalkboard SE', '"Chalkboard SE", Chalkboard'],
    ['Comic Sans MS', '"Comic Sans MS"'],
    ['Marker Felt', '"Marker Felt"'],
    ['Noteworthy', 'Noteworthy'],
    ['Bradley Hand', '"Bradley Hand"'],
    ['Snell Roundhand', '"Snell Roundhand"'],
    ['Papyrus', 'Papyrus'],
  ]],
];

/** 分组候选，已按本机实际安装情况过滤。isCJK 决定中文组还是西文组排前面。 */
export function fontGroups(isCJK) {
  const order = isCJK ? [['中文', CJK_GROUPS, true], ['西文', LATIN_GROUPS, false]]
                      : [['西文', LATIN_GROUPS, false], ['中文', CJK_GROUPS, true]];
  const out = [];
  for (const [prefix, groups, cjk] of order) {
    for (const [name, fallback, items] of groups) {
      const fonts = [];
      for (const [label, fam] of items) {
        if (!fontAvailable(fam, cjk)) continue;
        fonts.push({ label, value: `${fam}, ${fallback}`, fam });
      }
      if (fonts.length) out.push({ name: `${prefix} · ${name}`, fonts });
    }
  }
  return out;
}

/** 调参/自检用：列出被判为不可用的候选，确认没有把装了的字体误杀 */
export function unavailableFonts() {
  const out = [];
  for (const [groups, cjk] of [[CJK_GROUPS, true], [LATIN_GROUPS, false]]) {
    for (const [, , items] of groups) {
      for (const [label, fam] of items) {
        if (!fontAvailable(fam, cjk)) out.push(label);
      }
    }
  }
  return out;
}

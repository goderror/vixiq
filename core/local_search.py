# core/local_search.py
"""
本地以图搜图:ORB 特征 + HSV 颜色直方图融合评分。
从旧 local_search.py 迁移,改造点:
- library_path/feature_dir/cache_file 全走 paths.user_data(便携)
- weights/nfeatures/top_k/candidate_pool 读 config
- print → reporter;tqdm → reporter.progress
- tkinter filedialog 移到 GUI 层,本类只负责检索
- ★保留 cv_imread/cv_imread_gray(cv2.imdecode+np.fromfile 解决中文路径,勿改回 cv2.imread)
- 保留 safe_orb_match 的汉明距离降级、safe_compare_hist 的余弦相似度降级
- 保留 build_db 的增量构建 + 过期清理 + pickle 缓存失效逻辑
"""
import os
import pickle

import cv2
import numpy as np

from utils import paths
from utils.config import load_config
from utils.reporter import Reporter, PrintReporter


def _rel_to_str(rel) -> str:
    """npz 内嵌 rel 兼容转 str: savez 存 bytes, np.load 可能返回 np.bytes_ 或 ndarray。"""
    try:
        if isinstance(rel, bytes):
            return rel.decode('utf-8', 'replace')
        if hasattr(rel, 'item'):
            rel = rel.item()
            if isinstance(rel, bytes):
                return rel.decode('utf-8', 'replace')
            return str(rel)
        return str(rel)
    except Exception:
        return str(rel)


class QueryImageError(Exception):
    """查询图本身不可用 (读不出 / 特征不足)。与「索引失败」「没有匹配」分开报。"""


class ImageSearcher:
    def __init__(self, library_path: "str | None" = None,
                 reporter: "Reporter | None" = None,
                 config: "dict | None" = None,
                 progress_cb=None):
        self.config = config or load_config()
        self.reporter = reporter or PrintReporter()
        self.progress_cb = progress_cb
        lcfg = self.config["local_search"]
        self.library_path = library_path or lcfg["library_path"]

        self.feature_dir = paths.user_data(self.config["paths"]["feature_db_subdir"])
        self.orb = cv2.ORB_create(nfeatures=lcfg["orb_nfeatures"])
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        self.weight_orb = lcfg["weight_orb"]
        self.weight_color = lcfg["weight_color"]
        self.candidate_pool = lcfg["candidate_pool"]

        self.cache_file = os.path.join(self.feature_dir, "memory_cache.pkl")
        if not os.path.exists(self.feature_dir):
            os.makedirs(self.feature_dir)
        self.build_db(self.feature_dir, self.library_path)
        self._load_to_memory()

    def _emit(self, stage, done=0, total=0, outcome="", detail=""):
        cb = getattr(self, "progress_cb", None)
        if not cb:
            return
        try:
            cb(stage, done, total, outcome, detail)
        except Exception:
            pass                    # 进度回调坏了不能连累建库/检索

    def _load_to_memory(self):
        """一次性加载所有特征到内存,之后搜索不再碰磁盘"""
        self.db_des = []   # 所有 ORB 特征
        self.db_hist = []  # 所有颜色直方图
        self.db_names = []  # 文件名

        feat_files = sorted([f for f in os.listdir(self.feature_dir) if f.endswith(".npz")])

        if not feat_files:
            self.reporter.log("特征库为空,请先构建数据库")
            self._emit("load_done")
            return

        if os.path.exists(self.cache_file):
            self.reporter.log("从缓存加载...")
            self._emit("load_start", 0, 0, "", "缓存")
            try:
                with open(self.cache_file, 'rb') as f:
                    cache = pickle.load(f)
                    self.db_des = cache['des']
                    self.db_hist = cache['hist']
                    self.db_names = cache['names']
                self.reporter.log(f"缓存加载完成:{len(self.db_names)} 张图片")
                self._emit("load_done")
                return  # 缓存加载成功,跳过文件扫描
            except Exception as e:
                self.reporter.warn(f"缓存损坏,重新扫描文件: {e}")

        self.reporter.log(f"首次加载 {len(feat_files)} 个特征文件到内存...")
        total = len(feat_files)
        self._emit("load_plan", 0, total)
        for i, feat_file in enumerate(feat_files, 1):
            self.reporter.progress(i, total, "加载到内存")
            data = np.load(os.path.join(self.feature_dir, feat_file))
            self.db_des.append(data['des'])
            self.db_hist.append(data['color_hist'])
            # npz 内嵌 rel 相对路径 (本库图片在 X图/作者_ID/... 嵌套目录, 相对路径才能反查作品)
            self.db_names.append(_rel_to_str(data['rel']))
            self._emit("load", i, total)

        try:
            with open(self.cache_file, 'wb') as f:
                pickle.dump({
                    'des': self.db_des,
                    'hist': self.db_hist,
                    'names': self.db_names
                }, f)
            self.reporter.log(f"已保存内存缓存: {self.cache_file}")
        except Exception as e:
            self.reporter.warn(f"缓存保存失败: {e}")

        self.reporter.log(f"就绪:{len(self.db_names)} 张图片")
        self._emit("load_done")

    def cv_imread(self, file_path):
        """中文路径安全读图(cv2.imread 不支持中文,用 imdecode+fromfile 规避)"""
        return cv2.imdecode(np.fromfile(file_path, dtype=np.uint8), cv2.IMREAD_COLOR)

    def cv_imread_gray(self, file_path):
        return cv2.imdecode(np.fromfile(file_path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)

    def extract_color_hist(self, img, size=(64, 64)):
        if img is None or img.size == 0:
            return None
        try:
            img_resized = cv2.resize(img, size)
            hsv = cv2.cvtColor(img_resized, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1, 2], None, [16, 8, 8], [0, 180, 0, 256, 0, 256])
            cv2.normalize(hist, hist, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)
            return hist.flatten().astype(np.float32)
        except Exception as e:
            self.reporter.warn(f"颜色特征提取失败: {e}")
            return None

    def safe_orb_match(self, des1, des2):
        if des1 is None or des2 is None:
            return 0.0, 0
        if len(des1) == 0 or len(des2) == 0:
            return 0.0, 0

        if des1.dtype != np.uint8:
            des1 = des1.astype(np.uint8)
        if des2.dtype != np.uint8:
            des2 = des2.astype(np.uint8)

        if des1.ndim != 2 or des2.ndim != 2:
            return 0.0, 0

        # ORB 特征向量维度必须一致(32)
        if des1.shape[1] != des2.shape[1]:
            return 0.0, 0

        try:
            k = min(2, len(des2))
            if k < 2:
                # 数据库图片特征点只有1个,退化为普通匹配
                matches = self.bf.match(des1, des2)
                good_matches = [m for m in matches if m.distance < 50]
                raw_score = len(good_matches) / max(len(des1), 1)
                return min(1.0, raw_score ** 0.5), len(good_matches)

            matches = self.bf.knnMatch(des1, des2, k=k)
            good_matches = []
            for match_pair in matches:
                if len(match_pair) == 2:
                    m, n = match_pair
                    if float(m.distance) < 0.75 * float(n.distance):
                        good_matches.append(m)
                elif len(match_pair) == 1:
                    good_matches.append(match_pair[0])

            raw_score = len(good_matches) / max(len(des1), 1)
            normalized_score = min(1.0, raw_score ** 0.5)
            return normalized_score, len(good_matches)

        except cv2.error:
            # 降级:手动计算汉明距离
            try:
                sample_size = min(50, len(des1), len(des2))
                total_sim = 0
                for i in np.random.choice(len(des1), sample_size, replace=False):
                    xor_result = np.bitwise_xor(des1[i], des2).sum(axis=1)
                    min_dist = np.min(xor_result)
                    similarity = 1.0 - (min_dist / 256.0)
                    total_sim += similarity
                avg_score = total_sim / sample_size
                return avg_score, 0
            except Exception as e2:
                self.reporter.warn(f"降级匹配失败: {e2}")
                return 0.0, 0

    def safe_compare_hist(self, hist1, hist2):
        if hist1 is None or hist2 is None:
            return 0.0
        if hist1.shape != hist2.shape:
            return 0.0
        try:
            h1 = hist1.astype(np.float32).reshape(-1)
            h2 = hist2.astype(np.float32).reshape(-1)
        except Exception:
            return 0.0
        try:
            correlation = cv2.compareHist(h1, h2, cv2.HISTCMP_CORREL)
            return max(0.0, float(correlation))
        except Exception:
            # 降级:余弦相似度
            norm1 = np.linalg.norm(h1)
            norm2 = np.linalg.norm(h2)
            if norm1 == 0 or norm2 == 0:
                return 0.0
            return float(np.dot(h1, h2) / (norm1 * norm2))

    def compute_fusion_score(self, orb_score, color_score):
        fusion = self.weight_orb * orb_score + self.weight_color * color_score
        consistency = 1.0 - abs(orb_score - color_score)
        confidence_bonus = 0.1 * consistency
        final_score = fusion + confidence_bonus
        return min(1.0, final_score)

    @staticmethod
    def _file_sig(path):
        """文件身份: 大小 + 修改时间 (ns)。特征库用它判断已存特征是否对应当前文件。"""
        st = os.stat(path)
        return int(st.st_size), int(st.st_mtime_ns)

    def refresh(self, progress_cb=None) -> bool:
        """按当前图库更新特征库与内存索引, 返回是否有实际变化。

        已创建的单例在下一次检索前调用: 新增/变更文件按真实数量回调进度;
        图库无变化时不重载内存缓存, 保持快速检索。
        """
        old = self.progress_cb
        if progress_cb is not None:
            self.progress_cb = progress_cb
        try:
            changed = self.build_db(self.feature_dir, self.library_path)
            if changed or not self.db_names:
                self._load_to_memory()
            return bool(changed)
        except Exception:
            # 内存索引不能停留在半更新状态; 下次 refresh 会重新加载
            self.db_des, self.db_hist, self.db_names = [], [], []
            raise
        finally:
            self.progress_cb = old

    def build_db(self, feature_dir, library_path):
        if not library_path:
            self.reporter.log("未配置图库路径,跳过特征库构建")
            return False
        if not os.path.isdir(library_path):
            self.reporter.warn(f"图库路径不存在: {library_path}")
            return False

        valid_extensions = ('.jpg', '.png', '.jpeg', '.bmp', '.webp')
        # 递归收集 (本库图片在 X图/涩图/作者_ID/... 嵌套目录)
        all_files: "list[str]" = []
        for dirpath, _dirnames, fnames in os.walk(library_path):
            for fn in fnames:
                if fn.lower().endswith(valid_extensions):
                    rel = os.path.relpath(os.path.join(dirpath, fn), library_path)
                    all_files.append(rel.replace("\\", "/"))
        all_files_set = set(all_files)
        import hashlib as _hl

        # 清理过期特征文件 (npz 内嵌 rel 不在当前文件列表 → 删), 同时读出
        # 每份特征记录的文件身份。旧格式没有身份 → 下面按修改时间安全升级。
        existing = [f for f in os.listdir(feature_dir) if f.endswith('.npz')]
        removed_count = 0
        meta = {}   # rel -> (npz 文件名, 存下的身份或 None, npz 修改时间 ns)
        for f in existing:
            fp = os.path.join(feature_dir, f)
            sig = None
            try:
                with np.load(fp) as data:
                    rel = _rel_to_str(data['rel'])
                    if 'sig_size' in data.files and 'sig_mtime_ns' in data.files:
                        sig = (int(data['sig_size']), int(data['sig_mtime_ns']))
                npz_mtime = os.stat(fp).st_mtime_ns
            except Exception:
                rel = os.path.splitext(f)[0]  # 旧格式 (无 rel): 用文件名兜底
                npz_mtime = 0
            if rel not in all_files_set:
                try:
                    os.remove(fp)
                except OSError:
                    pass
                removed_count += 1
                continue
            meta[rel] = (f, sig, npz_mtime)
        if removed_count > 0:
            self.reporter.log(f"清理了 {removed_count} 个过期特征文件")

        # 判定新增与变更: 新格式逐项比较大小 + 修改时间; 旧格式没有身份时,
        # 只有图片比特征文件新才视为过期, 否则补齐身份 (安全升级, 不重复提取)。
        stale = {rel for rel in all_files if rel not in meta}
        upgrades = []
        for rel in all_files:
            info = meta.get(rel)
            if not info:
                continue
            fname, sig, npz_mtime = info
            img_path = os.path.join(library_path, *rel.split("/"))
            try:
                cur = self._file_sig(img_path)
            except OSError:
                stale.add(rel)      # 读不到 → 走提取, 由提取阶段报告 skip/fail
                continue
            if sig is None:
                if npz_mtime and cur[1] > npz_mtime:
                    stale.add(rel)
                else:
                    upgrades.append((rel, fname, cur))
            elif sig != cur:
                stale.add(rel)

        for rel, fname, cur in upgrades:
            fp = os.path.join(feature_dir, fname)
            try:
                with np.load(fp) as data:
                    des = data['des']
                    hist = data['color_hist']
                    relb = data['rel']
                np.savez(fp, des=des, color_hist=hist, rel=relb,
                         sig_size=np.int64(cur[0]), sig_mtime_ns=np.int64(cur[1]))
            except Exception:
                stale.add(rel)      # 升级失败 → 按过期重提取

        # 过期特征先删掉: 重提失败时旧特征绝不参与检索, 下次检索仍会重试
        for rel in stale:
            info = meta.get(rel)
            if info:
                try:
                    os.remove(os.path.join(feature_dir, info[0]))
                except OSError:
                    pass

        to_process = [rel for rel in all_files if rel in stale]
        if removed_count > 0 or len(to_process) > 0:
            # 有变动,删缓存强制重新加载
            if os.path.exists(self.cache_file):
                os.remove(self.cache_file)
                self.reporter.log("数据库有变动,清除旧缓存")
        if not to_process:
            self.reporter.log("特征库已是最新")
            self._emit("extract_plan", 0, 0)
            return bool(removed_count)

        total = len(to_process)
        self._emit("extract_plan", 0, total)
        self.reporter.log(f"处理 {total} 张新图片...")
        for i, rel in enumerate(to_process, 1):
            self.reporter.progress(i, total, "提取特征")
            outcome, detail = "fail", ""
            try:
                img_path = os.path.join(library_path, *rel.split("/"))
                fsig = self._file_sig(img_path)   # 读图前记录身份
                img_color = self.cv_imread(img_path)
                img_gray = self.cv_imread_gray(img_path)

                if img_color is None or img_gray is None:
                    outcome, detail = "skip", f"{rel}: 无法读取图片"
                    continue

                # 提取 ORB,失败则增强重试
                kp, des = self.orb.detectAndCompute(img_gray, None)
                if des is None or len(des) < 5:
                    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                    enhanced = clahe.apply(img_gray)
                    kp, des = self.orb.detectAndCompute(enhanced, None)

                color_hist = self.extract_color_hist(img_color)

                if des is not None and len(des) > 0 and color_hist is not None:
                    des = np.ascontiguousarray(des.astype(np.uint8))
                    color_hist = np.ascontiguousarray(color_hist.astype(np.float32))
                    np.savez(
                        os.path.join(feature_dir,
                                     _hl.md5(rel.encode("utf-8")).hexdigest() + ".npz"),
                        des=des,
                        color_hist=color_hist,
                        rel=rel.encode("utf-8"),
                        sig_size=np.int64(fsig[0]),
                        sig_mtime_ns=np.int64(fsig[1]),
                    )
                    outcome, detail = "ok", ""
                else:
                    outcome, detail = "skip", f"{rel}: 特征不足"
                    self.reporter.warn(f"跳过 {rel}: 特征不足")

            except Exception as e:
                outcome, detail = "fail", f"{rel}: {type(e).__name__}: {e}"
                self.reporter.warn(f"处理 {rel} 失败: {e}")
            finally:
                # 无论成功/跳过/失败, 这张都检查完了 —— N 只在这里加一次
                self._emit("extract", i, total, outcome, detail)
        return True

    def search(self, query_img_path, top_k: "int | None" = None,
               progress_cb=None, strict_query: bool = False) -> list:
        if top_k is None:
            top_k = self.config["local_search"]["top_k"]
        if progress_cb:
            try:
                progress_cb("search_start", 0, 0, "", "")
            except Exception:
                pass

        if not self.db_names:
            self.reporter.warn("特征库为空,无法搜索")
            return []

        query_color = self.cv_imread(query_img_path)
        query_gray = self.cv_imread_gray(query_img_path)

        if query_color is None or query_gray is None:
            self.reporter.error("无法读取目标图片")
            if strict_query:
                raise QueryImageError("无法读取目标图片（可能不是有效图片）")
            return []

        kp, des = self.orb.detectAndCompute(query_gray, None)
        if des is None or len(des) < 5:
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            enhanced = clahe.apply(query_gray)
            kp, des = self.orb.detectAndCompute(enhanced, None)
        if des is None or len(des) == 0:
            self.reporter.warn("ORB 特征不足")
            if strict_query:
                raise QueryImageError("查询图特征不足，无法检索")
            return []
        query_des = np.ascontiguousarray(des.astype(np.uint8))

        query_hist = self.extract_color_hist(query_color)
        if query_hist is None:
            self.reporter.warn("颜色特征提取失败")
            if strict_query:
                raise QueryImageError("查询图颜色特征提取失败")
            return []

        # 批量颜色匹配
        color_scores = np.array([
            self.safe_compare_hist(query_hist, h) for h in self.db_hist
        ])

        # 快速筛选 Top-N 候选 (小图库时候选池可能 >= 总数, argpartition 的 kth 会越界)
        n = min(self.candidate_pool, len(color_scores))
        if n == 0:
            self.reporter.warn("特征库为空")
            return []
        if n >= len(color_scores):
            candidate_indices = np.arange(len(color_scores))
        else:
            candidate_indices = np.argpartition(-color_scores, n)[:n]

        # 只对候选做 ORB 匹配
        results = []
        cands = list(candidate_indices)
        total = len(cands)
        for i, idx in enumerate(cands, 1):
            self.reporter.progress(i, total, "检索进度")
            orb_score, match_count = self.safe_orb_match(query_des, self.db_des[idx])
            color_score = float(color_scores[idx])
            fusion_score = self.compute_fusion_score(orb_score, color_score)
            results.append({
                "name": self.db_names[idx],
                "fusion_score": fusion_score,
                "orb_score": orb_score,
                "color_score": color_score,
                "match_count": match_count
            })
            if progress_cb:
                try:
                    progress_cb("search", i, total, "", "")
                except Exception:
                    pass

        if not results:
            self.reporter.warn("无匹配结果")
            return []

        results.sort(key=lambda x: x['fusion_score'], reverse=True)

        self.reporter.log(f"--- 前 {top_k} 个结果 ---")
        for i, res in enumerate(results[:top_k], 1):
            marker = " ⭐" if i == 1 else ""
            self.reporter.log(
                f"TOP {i}: {res['name']}{marker} | "
                f"融合:{res['fusion_score']:.3f} | "
                f"ORB:{res['orb_score']:.3f} | 颜色:{res['color_score']:.3f}"
            )

        return results[:top_k]

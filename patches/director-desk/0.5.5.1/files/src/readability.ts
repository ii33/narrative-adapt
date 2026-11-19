/**
 * 可读性层（narrative-adapt 补丁，不是上游功能）。
 *
 * 为什么单独一层，而不是散在 engine.ts 里：
 * 1. 页面的白模、房间盒子、1 米网格是给"编辑精度"用的；预演确认图要读的是
 *    「哪个是人物」「哪边是正面」「这间屋子多大」，两套目标混在一起谁都做不好。
 * 2. 抓帧走 engine.renderOutput（拍摄机位 + shotRenderer），DOM 标签不会进 PNG。
 *    所以这一层的几何必须挂在 scene 里、留在 layer 0 上——这也是「拍摄」视角的
 *    标签必须是 sprite，而布景视角仍然沿用页面原有 DOM 标签的原因。
 * 3. 必须能一键全撤：所有几何挂在自己的 group 上，关掉只是 visible=false 或复原材质；
 *    实体自己的 e.color、工程文件、页面的编辑交互一个字节都不改。
 */
import * as T from 'three';
import type { Entity, Project } from './model.ts';

export interface ReadabilityOptions {
    /** 人物头顶标志 + 脚下朝向箭头。 */
    persons?: boolean;
    /** 房间/场地轮廓线、1 米网格与尺寸标题。 */
    roomOutlines?: boolean;
    /** 按 kind 覆盖材质颜色（墙地／家具／人物／机位）。 */
    kindColors?: boolean;
    /** 实体名常显。 */
    labels?: boolean;
    /** 一次全开或全关；具体字段仍然优先（{all:false, labels:true} 只留标签）。 */
    all?: boolean;
}
export interface ReadabilityState { persons: boolean; roomOutlines: boolean; kindColors: boolean; labels: boolean }

const FIELDS = ['persons', 'roomOutlines', 'kindColors', 'labels'] as const;

/** 低饱和、彼此分得开的四组颜色：墙／地面、家具、人物、机位。 */
export const KIND_COLORS = { surface: '#cfc9bd', furniture: '#9db2bb', person: '#d79a5f', camera: '#8e9cc4' } as const;
const OUTLINE_COLOR = '#2f6f86';
const GRID_COLOR = '#4f7d8c';
const MARKER_COLOR = '#f2a93b';
/**
 * 标签外观：与 DOM 标签（`src/narrative/previs_theme.py` 的 `label_css()`）一字不差。
 * 为什么必须一致：布景视角看到的是 DOM 标签，抓帧 PNG 里是这张 canvas 贴图；
 * 两处各写一套，导出图就和屏幕上看到的不一样了。改这里记得同步 previs_theme.py。
 */
const LABEL_STYLE = {
    background: 'rgba(12,14,18,.86)',
    foreground: '#f2f4f6',
    border: 'rgba(242,244,246,.28)',
    fontWeight: 600,
    /** 贴图里文字占整条标签高度的比例。DOM：13px 字 / 21px 高 ≈ .62。 */
    textRatio: 40 / 64,
    /** CSS 圆角 4px 与 CSS 字号 13px 的比例，贴图按字号换算。 */
    radiusRatio: 4 / 13,
} as const;
/**
 * 字号（像素，指文字本身而不是整条标签）：与 engine.getProjectedLabels 同一条公式
 * ——REFERENCE 米处 BASE 像素，随距离缩小。下限 13px 与 DOM 侧一致（DOM 那边由
 * main.ts 夹到 13~15px）：再小就不是"看不清"，是不存在。
 */
const LABEL_PX = { base: 14, reference: 4, min: 13, max: 15 };
const LABEL_FADE = { start: 15, end: 32 };
/** 挤不开时让位的透明度：太淡等于没标，用户还是要读得出名字。 */
const LABEL_FADED_OPACITY = .62;
const MAX_LABELS = 64;

interface Rect { minX: number; maxX: number; minZ: number; maxZ: number; height: number }
interface MarkerNode { id: string; height: number; head: T.Mesh; arrow: T.Mesh }
interface LabelNode {
    id: string;
    /** 生成贴图时用的名字与强调色：改名／改色后要重画，否则标签挂着旧内容。 */
    text: string;
    accent: string;
    /** entity 跟模型根节点，camera 跟机位可视体（机位不进 models 表），fixed 是房间尺寸标题。 */
    follow: 'entity' | 'camera' | 'fixed';
    offsetY: number;
    anchor: T.Vector3;
    aspect: number;
    priority: number;
    sprite: T.Sprite;
    material: T.SpriteMaterial;
    texture: T.CanvasTexture;
}
interface ScreenBox { left: number; right: number; top: number; bottom: number }

const round1 = (value: number) => Math.round(value * 10) / 10;
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));
const overlaps = (a: ScreenBox, b: ScreenBox) => a.left < b.right - .5 && a.right > b.left + .5 && a.top < b.bottom - .5 && a.bottom > b.top + .5;

/** 网格统一走这里：每次重建都是新几何，重建前整组丢掉，不会漏显存。 */
function lineSegments(points: number[], color: string, opacity: number) {
    const geometry = new T.BufferGeometry();
    geometry.setAttribute('position', new T.Float32BufferAttribute(points, 3));
    // depthTest 关掉：被墙或家具挡住就失去"边界"的意义。
    const lines = new T.LineSegments(geometry, new T.LineBasicMaterial({ color, transparent: true, opacity, depthTest: false }));
    lines.renderOrder = 4;
    return lines;
}

/** 轮廓用扁盒子而不是 Line：WebGL 的 linewidth 恒为 1 像素，抓帧里会细得像铅笔灰。 */
function edgeBox(parent: T.Object3D, from: readonly [number, number], to: readonly [number, number], y: number, width: number, height: number, color: string, opacity: number) {
    const dx = to[0] - from[0], dz = to[1] - from[1], length = Math.hypot(dx, dz);
    if (length < 1e-4) return;
    const geometry = new T.BoxGeometry(length + width, height, width);
    const material = new T.MeshBasicMaterial({ color, transparent: true, opacity, depthTest: false, depthWrite: false });
    const mesh = new T.Mesh(geometry, material);
    mesh.position.set((from[0] + to[0]) / 2, y, (from[1] + to[1]) / 2);
    mesh.rotation.y = Math.atan2(-dz, dx);
    mesh.renderOrder = 4;
    parent.add(mesh);
}

/** 标签贴图：Canvas 画中文名，2 倍像素——抓帧是 960/1080 级别，1 倍会糊。 */
function labelTexture(text: string, accent: string) {
    const canvas = document.createElement('canvas');
    // 贴图按"高度 = 1 条标签"来画，字号与内边距全部按 textRatio 从高度换算：
    // 这样调一个数字，DOM 与 sprite 的观感一起变，不会各走各的。
    const height = 64;
    const fontSize = Math.round(height * LABEL_STYLE.textRatio);
    const font = `${LABEL_STYLE.fontWeight} ${fontSize}px "Noto Sans CJK SC","Source Han Sans SC","Microsoft YaHei",system-ui,sans-serif`;
    const measure = canvas.getContext('2d')!;
    measure.font = font;
    const width = clamp(Math.ceil(measure.measureText(text).width) + Math.round(fontSize * 1.05), 96, 560);
    canvas.width = width * 2; canvas.height = height * 2;
    const paint = canvas.getContext('2d')!;
    paint.scale(2, 2);
    paint.fillStyle = LABEL_STYLE.background;
    paint.beginPath();
    paint.roundRect(1, 1, width - 2, height - 2, Math.round(fontSize * LABEL_STYLE.radiusRatio));
    paint.fill();
    paint.strokeStyle = LABEL_STYLE.border;
    paint.lineWidth = 2;
    paint.stroke();
    // 左侧色条用实体自己的颜色：名字之外再给一条"这是谁"的线索。
    const barWidth = Math.round(height * .125), barTop = Math.round(height * .22);
    paint.fillStyle = accent;
    paint.beginPath();
    paint.roundRect(Math.round(height * .155), barTop, barWidth, height - barTop * 2, Math.round(barWidth / 2));
    paint.fill();
    const textLeft = Math.round(height * .4375);
    paint.fillStyle = LABEL_STYLE.foreground;
    paint.font = font;
    paint.textBaseline = 'middle';
    paint.fillText(text, textLeft, height / 2, width - textLeft - Math.round(height * .15));
    const texture = new T.CanvasTexture(canvas);
    texture.colorSpace = T.SRGBColorSpace;
    texture.minFilter = T.LinearFilter;
    texture.needsUpdate = true;
    return { texture, aspect: width / height };
}

/** 局部 +Z 为前方的扁三角形：朝向直接取模型根节点的 rotation.y（含 faceTarget／路径转身）。 */
function arrowGeometry() {
    const geometry = new T.BufferGeometry();
    const length = .34, half = .12;
    geometry.setAttribute('position', new T.Float32BufferAttribute([
        0, 0, length, -half, 0, 0, half, 0, 0,
        0, 0, length, half, 0, 0, 0, 0, -.07,
    ], 3));
    return geometry;
}

export class Readability {
    /** 默认全开：主程序工具条按需关；关掉即回到页面的原始观感。 */
    state: ReadabilityState = { persons: true, roomOutlines: true, kindColors: true, labels: true };
    readonly root = new T.Group();
    private outlines = new T.Group();
    private grid = new T.Group();
    private markers = new T.Group();
    private labels = new T.Group();
    private markerNodes = new Map<string, MarkerNode>();
    private labelNodes = new Map<string, LabelNode>();
    /** 每个实体"名字该浮多高"（相对模型根节点）：DOM 标签与 3D 标签共用，免得两处高度打架。 */
    private labelTops = new Map<string, number>();
    private originalColors = new Map<T.Material, number>();
    private rect: Rect | null = null;

    constructor() {
        this.root.name = 'readability';
        this.outlines.name = 'readability-outlines';
        this.grid.name = 'readability-grid';
        this.markers.name = 'readability-persons';
        this.labels.name = 'readability-labels';
        this.root.add(this.outlines, this.grid, this.markers, this.labels);
    }

    /** 接受部分字段 + {all:boolean}；未知键忽略（宿主多传字段不该让整页报错）。 */
    apply(value: unknown): ReadabilityState {
        const spec = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
        const next: ReadabilityState = { ...this.state };
        if (typeof spec.all === 'boolean') for (const key of FIELDS) next[key] = spec.all;
        for (const key of FIELDS) if (typeof spec[key] === 'boolean') next[key] = spec[key] as boolean;
        this.state = next;
        return next;
    }

    /** 实体名该浮多高；没有记录时给一个稳妥的默认值（DOM 标签用）。 */
    labelTop(entity: Entity) {
        return this.labelTops.get(entity.id) ?? (entity.kind === 'actor' || entity.kind === 'crowd' ? entity.height + .16 : entity.kind === 'camera' ? .28 : .6);
    }

    /** 这个名字在可读性层里有没有标签：地面一类"贴地大平板"不标，否则尺寸标题旁边会多出一堆废话。 */
    labeled(entity: Entity) {
        const node = this.labelNodes.get(entity.id);
        return !!node && node.follow !== 'fixed';
    }

    /**
     * 场景内容变了才重建（引擎 rebuild 调，且在 sample 之后：道具的包围盒要按已采样的姿态量）。
     * 为什么每次都先复原再上色：材质是新建的，留着上一轮的"原色"记录会把新材质的颜色写错。
     */
    sync(project: Project, models: Map<string, T.Object3D>, roomGroup: T.Object3D, cameraVisuals: Map<string, T.Object3D>) {
        this.labelTops.clear();
        this.buildOutline(project, models);
        this.buildMarkers(project);
        this.buildLabels(project, models);
        this.restoreColors();
        if (!this.state.kindColors) return;
        for (const entity of project.entities) {
            // 粒子／影响区／空间扭曲的颜色本身就是内容，不染。
            if (entity.visual || entity.field || entity.warp) continue;
            if (entity.kind === 'camera') {
                const visual = cameraVisuals.get(entity.id);
                if (visual) this.paint(visual, KIND_COLORS.camera);
            }
            // 人物颜色不走这里：colorHuman 每帧重新着色，见 engine.humanColor。
            else if (entity.kind === 'prop') {
                const root = models.get(entity.id);
                if (root) this.paint(root, KIND_COLORS.furniture);
            }
        }
        this.paint(roomGroup, KIND_COLORS.surface);
    }

    /** 每帧跟随：人物标志与朝向取采样后的模型根节点，所以路径、转身、脚底贴合都自动生效。 */
    sample(project: Project, models: Map<string, T.Object3D>, cameraVisuals: Map<string, T.Object3D>) {
        if (!this.state.persons && !this.state.labels) return;
        const byId = new Map(project.entities.map(entity => [entity.id, entity]));
        for (const node of this.markerNodes.values()) {
            const root = models.get(node.id);
            const shown = !!root && root.visible;
            node.head.visible = shown;
            node.arrow.visible = shown;
            if (!root || !shown) continue;
            // 头顶菱形：只用一个三角面太薄，八面体在各个视角都有体积。
            node.head.position.set(root.position.x, root.position.y + node.height + .22, root.position.z);
            const yaw = root.rotation.y, forwardX = Math.sin(yaw), forwardZ = Math.cos(yaw);
            node.arrow.position.set(root.position.x + forwardX * .34, .022, root.position.z + forwardZ * .34);
            node.arrow.rotation.y = yaw;
        }
        for (const node of this.labelNodes.values()) {
            if (node.follow === 'fixed') continue;
            const entity = byId.get(node.id);
            if (!entity || !entity.visible) { node.sprite.visible = false; continue; }
            if (node.follow === 'camera') {
                const visual = cameraVisuals.get(node.id);
                const at = visual && visual.visible ? visual.position : new T.Vector3(...entity.position);
                node.anchor.set(at.x, at.y + node.offsetY, at.z);
                continue;
            }
            const root = models.get(node.id);
            if (!root) { node.sprite.visible = false; continue; }
            node.anchor.set(root.position.x, root.position.y + node.offsetY, root.position.z);
        }
    }

    /** 每帧一次（prepareView 调）：视角相关的可见性，以及标签的投影／字号／避让。 */
    prepareView(editor: boolean, camera: T.PerspectiveCamera, width: number, height: number, renderable: boolean) {
        this.outlines.visible = renderable && this.state.roomOutlines;
        // 布景视图已经有页面自己的 1 米网格（helpers 层），这里只在拍摄视角／抓帧里补一份。
        this.grid.visible = renderable && this.state.roomOutlines && !editor;
        this.markers.visible = renderable && this.state.persons;
        this.labels.visible = renderable && this.state.labels && !editor;
        if (this.labels.visible) this.updateLabels(camera, width, height);
    }

    dispose() {
        this.restoreColors();
        this.clearObjects(this.outlines);
        this.clearObjects(this.grid);
        for (const node of this.markerNodes.values()) { this.disposeObject(node.head); this.disposeObject(node.arrow); }
        this.markerNodes.clear();
        for (const node of this.labelNodes.values()) this.dropLabel(node);
        this.labelNodes.clear();
        this.labelTops.clear();
        this.root.removeFromParent();
    }

    /* ------------------------------------------------------------ 房间轮廓 */

    private buildOutline(project: Project, models: Map<string, T.Object3D>) {
        this.clearObjects(this.outlines);
        this.clearObjects(this.grid);
        const room = project.room;
        let caption = '';
        if (room.enabled) {
            this.rect = { minX: -room.width / 2, maxX: room.width / 2, minZ: -room.depth / 2, maxZ: room.depth / 2, height: room.height };
            caption = `房间 ${round1(room.width)} × ${round1(room.depth)} m`;
        } else {
            // 纯盒子搭的预演场景没有 project.room 记录（接口 v1 也没有"改房间"这个操作），
            // 那就按可见道具的实际占地量一遍：量出来的尺寸比让人手填更接近"这间屋子到底多大"。
            const bounds = new T.Box3();
            let counted = 0;
            for (const entity of project.entities) {
                if (entity.kind !== 'prop' || entity.light || entity.visual || entity.field || entity.warp || !entity.visible) continue;
                const root = models.get(entity.id);
                if (!root) continue;
                root.updateWorldMatrix(true, true);
                bounds.expandByObject(root);
                counted++;
            }
            this.rect = null;
            if (counted) {
                const size = bounds.getSize(new T.Vector3());
                if (size.x > .5 && size.z > .5 && size.x < 200 && size.z < 200) {
                    this.rect = { minX: bounds.min.x, maxX: bounds.max.x, minZ: bounds.min.z, maxZ: bounds.max.z, height: Math.max(.6, bounds.max.y) };
                    caption = `场地 ${round1(size.x)} × ${round1(size.z)} m`;
                }
            }
        }
        const rect = this.rect;
        if (!rect) return;
        // 地面一圈 + 顶部一圈 + 四根竖线：竖线高度取房间层高，纯盒子场景取最高道具，
        // 这样"这是一个有高度的屋子"在斜俯视里也看得出来。
        const floorY = .012, top = clamp(rect.height, .8, 8), width = .022;
        const corners: [number, number][] = [[rect.minX, rect.minZ], [rect.maxX, rect.minZ], [rect.maxX, rect.maxZ], [rect.minX, rect.maxZ]];
        for (let index = 0; index < 4; index++) {
            const from = corners[index], to = corners[(index + 1) % 4];
            edgeBox(this.outlines, from, to, floorY + .004, width, .008, OUTLINE_COLOR, .95);
            edgeBox(this.outlines, from, to, top, width, .008, OUTLINE_COLOR, .7);
            const [x, z] = from;
            const post = new T.Mesh(new T.BoxGeometry(width, top, width), new T.MeshBasicMaterial({ color: OUTLINE_COLOR, transparent: true, opacity: .8, depthTest: false, depthWrite: false }));
            post.position.set(x, top / 2, z);
            post.renderOrder = 4;
            this.outlines.add(post);
        }

        // 1 米网格：只在量出来的范围里画，超过 60 格不画（铺满整片会变成噪点）。
        const columns = Math.round((rect.maxX - rect.minX) / 1), rows = Math.round((rect.maxZ - rect.minZ) / 1);
        if (columns >= 1 && rows >= 1 && columns <= 60 && rows <= 60) {
            const grid: number[] = [];
            for (let index = 1; index < columns; index++) { const x = rect.minX + index; grid.push(x, floorY, rect.minZ, x, floorY, rect.maxZ); }
            for (let index = 1; index < rows; index++) { const z = rect.minZ + index; grid.push(rect.minX, floorY, z, rect.maxX, floorY, z); }
            if (grid.length) this.grid.add(lineSegments(grid, GRID_COLOR, .22));
        }

        // 尺寸标题：放在地面中央，压不压得上交给标签避让；priority 最高，先占位。
        const captionNode = this.makeLabel('room-caption', caption, OUTLINE_COLOR, 0, 9);
        captionNode.follow = 'fixed';
        captionNode.anchor.set((rect.minX + rect.maxX) / 2, floorY + .12, (rect.minZ + rect.maxZ) / 2);
    }

    /* ------------------------------------------------------------ 人物标志 */

    private buildMarkers(project: Project) {
        const keep = new Set<string>();
        for (const entity of project.entities) {
            if (entity.kind !== 'actor' || !entity.visible) continue;
            keep.add(entity.id);
            const existing = this.markerNodes.get(entity.id);
            if (existing) { existing.height = entity.height; continue; }
            // 头顶菱形 + 脚下箭头：盒子里的人物没有脸也没有朝向，这两件是唯一能在缩略尺寸上讲清"谁、朝哪边"的东西。
            const head = new T.Mesh(new T.OctahedronGeometry(.075), this.markerMaterial(MARKER_COLOR));
            const arrow = new T.Mesh(arrowGeometry(), this.markerMaterial(MARKER_COLOR, .85));
            (arrow.material as T.MeshBasicMaterial).side = T.DoubleSide;
            head.visible = arrow.visible = false;
            this.markers.add(head, arrow);
            this.markerNodes.set(entity.id, { id: entity.id, height: entity.height, head, arrow });
        }
        for (const [id, node] of this.markerNodes) {
            if (keep.has(id)) continue;
            this.disposeObject(node.head);
            this.disposeObject(node.arrow);
            this.markerNodes.delete(id);
        }
    }

    private markerMaterial(color: string, opacity = .95) {
        // depthTest 关掉：人物站在家具后面时标志仍要看得见（这正是它存在的理由）。
        const material = new T.MeshBasicMaterial({ color, transparent: true, opacity, depthTest: false, depthWrite: false, toneMapped: false });
        material.name = 'readability-person';
        return material;
    }

    /* ------------------------------------------------------------ 标签 */

    private buildLabels(project: Project, models: Map<string, T.Object3D>) {
        const wanted = new Set<string>();
        const boxes = new Map<string, T.Box3>();
        for (const entity of project.entities) {
            if (!entity.visible || entity.light) continue;
            const text = (entity.name.split(' · ')[0] || entity.name).trim();
            if (!text) continue;
            // 名字浮在实体顶上：道具按包围盒顶，人物按身高，机位按站位加一点。
            const top = entity.kind === 'actor' || entity.kind === 'crowd' ? entity.height + .16 : entity.kind === 'camera' ? .28 : this.measureTop(entity, models, boxes);
            // 贴地的平板（地面、地毯、台面…）不是"可以点名"的对象：标了只会挤在尺寸标题旁边。
            if (entity.kind === 'prop' && top < .25) continue;
            const offsetY = entity.kind === 'prop' ? clamp(top + .08, .1, 12) : top;
            wanted.add(entity.id);
            this.labelTops.set(entity.id, offsetY);
            const existing = this.labelNodes.get(entity.id);
            if (existing) {
                // 改名、改色之后贴图必须重画，否则标签还挂着旧名字。
                existing.offsetY = offsetY;
                if (existing.text === text && existing.accent === entity.color) continue;
            }
            const node = this.makeLabel(entity.id, text, entity.color, offsetY, 3);
            node.follow = entity.kind === 'camera' ? 'camera' : 'entity';
        }
        for (const [id, node] of this.labelNodes) {
            // 尺寸标题不跟实体走，别在清理里被误删。
            if (wanted.has(id) || node.follow === 'fixed') continue;
            this.dropLabel(node);
            this.labelNodes.delete(id);
        }
    }

    private measureTop(entity: Entity, models: Map<string, T.Object3D>, boxes: Map<string, T.Box3>) {
        const root = models.get(entity.id);
        if (!root) return .6;
        let box = boxes.get(entity.id);
        if (!box) {
            root.updateWorldMatrix(true, true);
            box = new T.Box3().setFromObject(root);
            boxes.set(entity.id, box);
        }
        // 返回"模型根节点上方多高是包围盒顶"，调用方自己决定加多少留白、以及值不值得标。
        const top = box.max.y - root.position.y;
        return Number.isFinite(top) && top > .05 ? top : .6;
    }

    private makeLabel(id: string, text: string, accent: string, offsetY: number, priority: number) {
        const previous = this.labelNodes.get(id);
        if (previous) { this.dropLabel(previous); this.labelNodes.delete(id); }
        const { texture, aspect } = labelTexture(text, accent);
        const material = new T.SpriteMaterial({ map: texture, transparent: true, depthTest: false, depthWrite: false, toneMapped: false });
        const sprite = new T.Sprite(material);
        sprite.renderOrder = 1000;
        sprite.visible = false;
        this.labels.add(sprite);
        const node: LabelNode = { id, text, accent, follow: 'entity', offsetY, anchor: new T.Vector3(), aspect, priority, sprite, material, texture };
        this.labelNodes.set(id, node);
        return node;
    }

    private dropLabel(node: LabelNode) {
        node.sprite.removeFromParent();
        node.material.dispose();
        node.texture.dispose();
    }

    /**
     * 标签的投影、字号、避让。
     * 字号随距离缩小，压到 LABEL_PX.min 就不再缩；投影重叠时先试"往上错开"，
     * 错不开就按最近距离淡出——比一堆字压在一起更难读。
     */
    private updateLabels(camera: T.PerspectiveCamera, width: number, height: number) {
        if (!width || !height) return;
        const tanHalf = Math.tan(T.MathUtils.degToRad(camera.fov * .5));
        const up = new T.Vector3(0, 1, 0).applyQuaternion(camera.quaternion);
        const heading = camera.getWorldDirection(new T.Vector3());
        const rows: { node: LabelNode; screenX: number; screenY: number; distance: number; px: number; worldPerPixel: number }[] = [];
        for (const node of this.labelNodes.values()) {
            node.sprite.visible = false;
            const toAnchor = node.anchor.clone().sub(camera.position);
            const distance = toAnchor.length();
            // 在相机背后、贴到镜头上、或远到只剩噪点：都不画。
            if (distance < .25 || distance > LABEL_FADE.end || toAnchor.dot(heading) <= .05) continue;
            const projected = node.anchor.clone().project(camera);
            if (projected.z < -1 || projected.z > 1) continue;
            const screenX = (projected.x * .5 + .5) * width, screenY = (-projected.y * .5 + .5) * height;
            if (screenX < -width * .3 || screenX > width * 1.3 || screenY < -height * .3 || screenY > height * 1.3) continue;
            // LABEL_PX 给的是"文字像素"，而 px 是整条标签的屏幕高度，要按 textRatio 反算回来：
            // DOM 标签那边同样是 13~15px 的字，两边才谈得上"字号比例一致"。
            const px = clamp(LABEL_PX.base * LABEL_PX.reference / distance, LABEL_PX.min, LABEL_PX.max) / LABEL_STYLE.textRatio;
            rows.push({ node, screenX, screenY, distance, px, worldPerPixel: 2 * tanHalf * distance / height });
        }
        // 尺寸标题优先保住，其次近处的人／物。
        rows.sort((left, right) => right.node.priority - left.node.priority || left.distance - right.distance);
        const placed: ScreenBox[] = [];
        for (const row of rows) {
            if (placed.length >= MAX_LABELS) break;
            const halfWidth = row.px * row.node.aspect * .5, halfHeight = row.px * .5;
            const step = row.px + 3;
            let offset = 0, faded = false;
            // 只做"往上错开"：往下会压到脚，往上永远还有空间。
            for (const attempt of [0, 1, 2, 3, 4, 5, 6]) {
                offset = attempt * step;
                const box = { left: row.screenX - halfWidth, right: row.screenX + halfWidth, top: row.screenY - offset - halfHeight, bottom: row.screenY - offset + halfHeight };
                if (!placed.some(other => overlaps(box, other))) { placed.push(box); faded = false; break; }
                faded = true;
            }
            if (faded) offset = 0;
            const node = row.node;
            const fade = row.distance > LABEL_FADE.start ? 1 - (row.distance - LABEL_FADE.start) / (LABEL_FADE.end - LABEL_FADE.start) : 1;
            node.material.opacity = faded ? LABEL_FADED_OPACITY : clamp(fade, .18, 1);
            const scaleY = row.px * row.worldPerPixel;
            node.sprite.scale.set(scaleY * node.aspect, scaleY, 1);
            node.sprite.position.copy(node.anchor).addScaledVector(up, offset * row.worldPerPixel);
            node.sprite.visible = true;
        }
    }

    /* ------------------------------------------------------------ 颜色覆盖 */

    private paint(object: T.Object3D, color: string) {
        object.traverse(child => {
            const material = (child as T.Mesh).material as T.Material | T.Material[] | undefined;
            if (!material) return;
            for (const entry of Array.isArray(material) ? material : [material]) {
                const tint = (entry as T.MeshStandardMaterial).color;
                if (!tint || typeof tint.set !== 'function' || typeof tint.getHex !== 'function') continue;
                // 只记第一次：同一份材质被两个实体共用时，复原要回到真正的原色。
                if (!this.originalColors.has(entry)) this.originalColors.set(entry, tint.getHex());
                tint.set(color);
            }
        });
    }

    /** 复原所有被覆盖过的材质颜色。重建后的旧材质已 dispose，这里写回颜色没有副作用。 */
    private restoreColors() {
        for (const [material, hex] of this.originalColors) (material as T.MeshStandardMaterial).color?.setHex(hex);
        this.originalColors.clear();
    }

    /* ------------------------------------------------------------ 显存 */

    private clearObjects(group: T.Object3D) {
        while (group.children.length) this.disposeObject(group.children[0]);
    }

    private disposeObject(object: T.Object3D) {
        object.removeFromParent();
        object.traverse(child => {
            const mesh = child as T.Mesh;
            mesh.geometry?.dispose?.();
            const material = mesh.material as T.Material | T.Material[] | undefined;
            for (const entry of Array.isArray(material) ? material : material ? [material] : []) entry.dispose();
        });
    }
}

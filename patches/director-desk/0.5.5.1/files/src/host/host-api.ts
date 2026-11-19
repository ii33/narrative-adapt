/**
 * 预演页对主程序的接口层（协议 v1，契约见 docs/PREVIS_HOST_API.md）。
 *
 * 三条边界，决定了这个文件的形状：
 * 1. **接口本体在页面内**：方法直接操作页面自己的 AppContext / Engine / 历史事务，
 *    不读写 DOM、不碰 `.director` 文件；宿主（QtWebEngine）注入的引导脚本只确认它挂上了。
 * 2. **不拖慢首屏**：本文件在 startup.ts 里就被挂上，所以运行时导入一律是 `import type`
 *    或按需 `import()`——三维与资产目录那些大模块等真正调用接口时才加载。
 * 3. **调用永不抛裸异常**：契约要求失败返回 `{error: 可读原因}`，所有入口都包在 attempt() 里。
 */
import type { Engine } from '../engine.ts';
import type { Project } from '../model.ts';

/** 协议版本：主程序用它判断兼容性，改动语义就要 +1。 */
export const PREVIS_PROTOCOL = 1;

/**
 * 页面把这些钩子交给接口层。为什么要注入而不是直接 import main.ts：
 * 接口层不该知道“当前戏段”是哪个模块级变量，main.ts 才是它的主人；
 * 这样也不会出现 host-api ↔ main 的循环依赖。
 */
export interface PrevisHostRuntime {
    project(): Project;
    engine(): Engine;
    sceneId(): string;
    revision(): number;
    timeMs(): number;
    appVersion(): string;
    /** 页面忙（拖动／导出／AI 任务）时返回可读原因；空串表示可以接受接口的写操作。 */
    blockedReason(): string;
    /** 事务提交：整份新 project 通过全量校验才生效，失败抛出可读原因且不留痕。 */
    commit(next: Project, label: string): void;
    /** 只移动播放头（秒），不改变其它播放状态。 */
    seek(seconds: number): void;
}

type Listener = (event: ChangeEvent) => void;
interface ChangeEvent { sceneId: string; revision: number; entityCount: number; duration_ms: number; time_ms: number; source: 'api' | 'ui' }
type Failure = { ok: false; error: string };

let runtime: PrevisHostRuntime | null = null;
let bootstrapped = false;
let installed = false;
let readySettled = false;
let resolveReady!: () => void;
const ready = new Promise<void>(resolve => { resolveReady = resolve; });
const listeners: Listener[] = [];
/** 变更来源：接口自己提交的改动标 api，其余（页面上手动拖、撤销等）标 ui。 */
let pendingSource: '' | 'api' = '';
let watching = false;
let lastRevision = -1;

const reason = (error: unknown): string => (error instanceof Error ? error.message : String(error ?? '未知错误'));

/** 页面初始化完成前到达的调用在这里等一会儿，而不是立刻报错——宿主可能比首帧更早发问。 */
function waitReady(timeoutMs = 20000): Promise<void> {
    if (readySettled) return Promise.resolve();
    return new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('预演页尚未初始化完成（等待超时）')), timeoutMs);
        void ready.then(() => { clearTimeout(timer); resolve(); });
    });
}

async function attempt<T extends object>(run: () => Promise<T>): Promise<T | Failure> {
    try {
        await waitReady();
        return await run();
    } catch (error) {
        return { ok: false, error: reason(error) };
    }
}

function requireRuntime(): PrevisHostRuntime {
    if (!runtime) throw new Error('预演页尚未初始化完成');
    return runtime;
}

/** 写操作前先问页面：正在拖动／导出时改场景会和用户的手抢同一份数据。 */
function requireWritable(): PrevisHostRuntime {
    const host = requireRuntime();
    const blocked = host.blockedReason();
    if (blocked) throw new Error(blocked);
    return host;
}

async function draft(): Promise<Project> {
    const { clone } = await import('../model.ts');
    return clone(requireRuntime().project());
}

/**
 * 统一提交：所有写操作都在副本上做完再交出去，页面侧一次校验、一次重建、一次入历史，
 * 所以批量失败不会留下中间态（见 main.ts 的 previsCommit）。
 */
function commitDraft(host: PrevisHostRuntime, next: Project, label: string): void {
    const before = host.revision();
    pendingSource = 'api';
    try {
        host.commit(next, label);
    } catch (error) {
        pendingSource = '';
        throw error;
    }
    // 提交被页面的“内容没变”判断吃掉（revision 不动）时，别把来源标记留给下一次页面内编辑。
    if (host.revision() === before) pendingSource = '';
}

const asRecord = (value: unknown): Record<string, unknown> =>
    value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};

/** 允许 `updateEntity({id, patch})` 这种把参数摊在一个对象里的写法：去掉外层自己的键再当 patch。 */
function withoutKeys(source: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
    const rest: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(source)) if (!keys.includes(key)) rest[key] = value;
    return rest;
}

/* ------------------------------------------------------------ 变更通知 */

function notify(): void {
    const host = runtime;
    if (!host) return;
    let event: ChangeEvent;
    try {
        const project = host.project();
        event = {
            sceneId: host.sceneId(),
            revision: host.revision(),
            entityCount: project.entities.length,
            duration_ms: Math.round(project.duration * 1000),
            time_ms: host.timeMs(),
            source: pendingSource === 'api' ? 'api' : 'ui',
        };
    } catch { return; }
    pendingSource = '';
    for (const listener of [...listeners]) {
        // 主程序一侧的回调出错不能影响页面，也不能挡住其它监听者。
        try { listener(event); } catch { /* 忽略 */ }
    }
}

/**
 * 每帧看一次页面的 revision：页面上手动拖动、撤销、批量修改都会 +1，
 * 这样“人工微调也要通知主程序”就自然成立，而通知本身天然被节流到每帧最多一次。
 */
function watchRevision(): void {
    if (watching) return;
    watching = true;
    const tick = () => {
        requestAnimationFrame(tick);
        if (!runtime) return;
        let revision = -1;
        try { revision = runtime.revision(); } catch { return; }
        if (revision === lastRevision) return;
        lastRevision = revision;
        notify();
    };
    requestAnimationFrame(tick);
}

/* ------------------------------------------------------------ 抓帧 */

/**
 * 抓一帧：用页面自己的输出管线（engine.renderOutput）离屏渲染到同一个 renderer 的
 * 绘图缓冲，再读成 PNG。为什么不用第二个 WebGLRenderer：材质、阴影缓存、光效、
 * 墙面透传都挂在现有 renderer 上，另起一套会得到一张“不一样”的图，就失去了确认意义。
 * 代价是它会临时改 engine.time 与取景画布尺寸，所以结束后立刻把这两样恢复回去，
 * 播放状态（playing／播放头）不因此改变。
 */
async function captureFrame(value: unknown) {
    const host = requireRuntime();
    const engine = host.engine();
    if (engine.exporting) throw new Error('正在导出视频，暂时无法抓帧');
    if (engine.dragging) throw new Error('正在拖动对象，请先松手再抓帧');
    const spec = asRecord(value);
    const project = host.project();
    const { outputSize } = await import('../model.ts');
    const [defaultWidth, defaultHeight] = outputSize(project.aspect, 1280);
    const dimension = (raw: unknown, fallback: number, field: string) => {
        if (raw === undefined || raw === null) return fallback;
        if (typeof raw !== 'number' || !Number.isFinite(raw) || raw < 64 || raw > 4096) throw new Error(`${field} 须为 64—4096 的整数`);
        return Math.round(raw);
    };
    const width = dimension(spec.width, defaultWidth, 'width');
    const height = dimension(spec.height, defaultHeight, 'height');
    const cameraId = spec.cameraId === undefined || spec.cameraId === '' ? 'program' : String(spec.cameraId);
    if (cameraId !== 'program' && !project.entities.some(e => e.id === cameraId && e.kind === 'camera')) throw new Error(`机位不存在：${cameraId}`);
    const at = timeSeconds(host, project, spec.time_ms ?? spec.timeMs);
    const previous = engine.time;
    try {
        // 先把异步贴图（视频表面等）备好，再在同一个 tick 里渲染，避免中间被帧循环插进来。
        await engine.prepareOutput(at);
        if (engine.exporting) throw new Error('正在导出视频，暂时无法抓帧');
        const canvas = engine.renderOutput(at, width, height, cameraId);
        const url = canvas.toDataURL('image/png');
        // 只回纯 base64：契约字段名就叫 png_base64，前缀由调用方按需自己拼。
        const base64 = url.slice(url.indexOf(',') + 1);
        return { ok: true, png_base64: base64, width, height, cameraId, time_ms: Math.round(at * 1000) };
    } finally {
        // 复原播放头与取景画布尺寸。不用 restorePreview：它会把 exporting 置回 false，
        // 万一抓帧期间导出抢了进来，那一手会直接毁掉正在编码的帧。
        if (!engine.exporting) {
            engine.sample(previous);
            engine.requestResize();
            engine.render();
        }
    }
}

function timeSeconds(host: PrevisHostRuntime, project: Project, value: unknown): number {
    const raw = value === undefined || value === null ? host.timeMs() : value;
    if (typeof raw !== 'number' || !Number.isFinite(raw)) throw new Error('time_ms 须为有限数字（毫秒）');
    const seconds = raw / 1000;
    if (seconds < 0 || seconds > project.duration + 1e-6) throw new Error(`time_ms 须在 0 到 ${Math.round(project.duration * 1000)} 之间`);
    return Math.min(seconds, project.duration);
}

/* ------------------------------------------------------------ 方法表 */

/** 引导：宿主注入的小脚本调用它，确认接口可用；重复调用无副作用。 */
function boot() {
    bootstrapped = true;
    return {
        ok: !!runtime,
        protocol: PREVIS_PROTOCOL,
        appVersion: runtime ? runtime.appVersion() : '',
        ready: !!runtime,
        bootstrapped,
        ...(runtime ? {} : { error: '预演页尚未初始化完成' }),
    };
}

const api = {
    /** 协议号也挂在接口对象上：重复安装（页面重载、宿主重复注入）时据此认出来。 */
    protocol: PREVIS_PROTOCOL,
    boot,
    hello: () => attempt(async () => {
        const host = runtime;
        return {
            ok: true,
            protocol: PREVIS_PROTOCOL,
            appVersion: host ? host.appVersion() : '',
            ready: !!host,
            bootstrapped,
            sceneId: host ? host.sceneId() : '',
            revision: host ? host.revision() : -1,
            ...(host ? {} : { error: '预演页尚未初始化完成' }),
        };
    }),

    getScene: () => attempt(async () => {
        const host = requireRuntime();
        const { sceneSnapshot } = await import('./host-scene.ts');
        return { ok: true, ...sceneSnapshot(host.project(), host.sceneId(), host.revision()) };
    }),

    listCameras: async () => {
        // 契约里这个方法直接返回数组（不是 {ok, cameras}）：主程序按数组读，失败时才拿到 {error}。
        try {
            await waitReady();
            const host = requireRuntime();
            const { cameraList } = await import('./host-scene.ts');
            return cameraList(host.project());
        } catch (error) {
            return { ok: false, error: reason(error) };
        }
    },

    addEntity: (spec: unknown) => attempt(async () => {
        const host = requireWritable();
        const { addEntityTo } = await import('./host-scene.ts');
        const next = await draft();
        const added = addEntityTo(next, asRecord(spec).spec ?? spec);
        commitDraft(host, next, `接口新增对象：${added.name}`);
        return { ok: true, id: added.id, kind: added.kind, name: added.name, asset: added.asset };
    }),

    updateEntity: (idOrSpec: unknown, maybePatch?: unknown) => attempt(async () => {
        const host = requireWritable();
        const { updateEntityIn } = await import('./host-scene.ts');
        const spec = asRecord(idOrSpec);
        const id = typeof idOrSpec === 'string' ? idOrSpec : spec.id;
        const patch = typeof idOrSpec === 'string' ? maybePatch : (spec.patch ?? withoutKeys(spec, ['id', 'patch']));
        const next = await draft();
        const updated = updateEntityIn(next, id, patch);
        commitDraft(host, next, `接口修改对象：${updated.name}`);
        return { ok: true, id: updated.id };
    }),

    removeEntity: (idOrSpec: unknown) => attempt(async () => {
        const host = requireWritable();
        const { removeEntityFrom } = await import('./host-scene.ts');
        const id = typeof idOrSpec === 'string' ? idOrSpec : asRecord(idOrSpec).id;
        const next = await draft();
        const result = removeEntityFrom(next, id);
        commitDraft(host, next, '接口删除对象');
        return { ok: true, ...result };
    }),

    setCamera: (idOrSpec: unknown, maybeSpec?: unknown) => attempt(async () => {
        const host = requireWritable();
        const { setCameraIn } = await import('./host-scene.ts');
        const record = asRecord(idOrSpec);
        const id = typeof idOrSpec === 'string' ? idOrSpec : record.id;
        const spec = typeof idOrSpec === 'string' ? maybeSpec : (record.spec ?? withoutKeys(record, ['id', 'spec']));
        const next = await draft();
        const result = setCameraIn(next, id, spec);
        commitDraft(host, next, '接口调整机位');
        return { ok: true, ...result };
    }),

    setCuts: (cutsOrSpec: unknown) => attempt(async () => {
        const host = requireWritable();
        const { setCutsIn } = await import('./host-scene.ts');
        const spec = asRecord(cutsOrSpec);
        const next = await draft();
        const result = setCutsIn(next, Array.isArray(cutsOrSpec) ? cutsOrSpec : (spec.cuts ?? cutsOrSpec));
        commitDraft(host, next, '接口设置切镜');
        return { ok: true, ...result };
    }),

    /**
     * 可读性总开关（narrative-adapt 补丁）：只改显示，不改场景数据、不进工程文件、不进撤销栈。
     * options = {persons, roomOutlines, kindColors, labels}，默认全开；也接受 {all:false} 一次全关
     * （工具条"一眼看清"按钮的对照状态靠它）。返回 {ok, applied}，applied 是生效后的四项取值。
     */
    setReadability: (options: unknown) => attempt(async () => {
        const host = requireRuntime();
        const engine = host.engine() as { setReadability?: (value: unknown) => unknown };
        if (typeof engine.setReadability !== 'function') throw new Error('当前页面不支持可读性开关');
        return { ok: true, applied: engine.setReadability(options ?? {}) };
    }),

    setTime: (value: unknown) => attempt(async () => {
        const host = requireWritable();
        const raw = typeof value === 'number' ? value : (asRecord(value).time_ms ?? asRecord(value).timeMs ?? value);
        const at = timeSeconds(host, host.project(), raw);
        host.seek(at);
        return { ok: true, time_ms: Math.round(at * 1000) };
    }),

    captureFrame: (spec: unknown) => attempt(async () => captureFrame(spec)),

    onChange: (callback: unknown) => attempt(async () => {
        requireRuntime();
        if (typeof callback !== 'function') throw new Error('onChange 需要一个回调函数');
        if (listeners.length >= 16) throw new Error('onChange 监听器过多（上限 16 个）');
        listeners.push(callback as Listener);
        watchRevision();
        return { ok: true, listeners: listeners.length };
    }),

    /**
     * 批量事务：在副本上逐条应用（每条都校验 id 与字段），全部通过才提交一次；
     * 任何一条失败整批不生效，并报出是第几条、什么原因。提交前页面侧还会做一次全量校验，
     * 所以“单条合法但组合起来不合法”（例如删掉最后一台机位）也会被拦下。
     */
    applyBatch: (value: unknown) => attempt(async () => {
        const host = requireWritable();
        const { applyOperation, batchFailure } = await import('./host-scene.ts');
        const operations = Array.isArray(value) ? value : asRecord(value).ops;
        if (!Array.isArray(operations)) throw new Error('applyBatch 需要操作数组 [{op: "add" | "update" | "remove" | "setCamera" | "setCuts", ...}]');
        if (!operations.length) return { ok: true, results: [] };
        const next = await draft();
        const results: unknown[] = [];
        for (let index = 0; index < operations.length; index++) {
            const op = String(asRecord(operations[index]).op ?? '');
            try {
                results.push(applyOperation(next, operations[index], index));
            } catch (error) {
                throw new Error(batchFailure(error, index, op));
            }
        }
        try {
            commitDraft(host, next, `接口批量修改 ${operations.length} 条`);
        } catch (error) {
            throw new Error(`整批未生效：${reason(error)}`);
        }
        return { ok: true, results };
    }),
};

export type PrevisHostApi = typeof api;

/* ------------------------------------------------------------ 安装与绑定 */

/** 尽早挂上 window.__narrativePrevisHost：即使三维引擎还没起来，hello() 也能回话。 */
export function installPrevisHostApi(): void {
    if (installed) return;
    const scope = window as unknown as Record<string, unknown>;
    const existing = scope.__narrativePrevisHost as { protocol?: unknown } | undefined;
    if (existing && existing.protocol === PREVIS_PROTOCOL) return;
    installed = true;
    scope.__narrativePrevisHost = api;
}

/**
 * main.ts 在页面装配完成后调用：交出“怎么读、怎么改”的钩子。
 * 这里不复制任何业务逻辑——事务语义（历史、校验、重建、revision）仍然只有页面一份。
 */
export function bindPrevisHostRuntime(host: PrevisHostRuntime): void {
    runtime = host;
    lastRevision = host.revision();
    readySettled = true;
    resolveReady();
    watchRevision();
    installPrevisHostApi();
}

/** 调试/冒烟用：当前监听器数量。 */
export function previsHostListenerCount(): number { return listeners.length; }

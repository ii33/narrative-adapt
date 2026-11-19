/**
 * 预演接口的场景层：Project ↔ 契约 JSON 的翻译，以及接口写操作的全部语义。
 *
 * 为什么单独一层：接口本体（host-api.ts）只负责挂 window、串行化调用、抓帧与变更通知；
 * 场景的读写集中在这里，能对着 docs/PREVIS_HOST_API.md 逐条核对，也保证所有写操作
 * 都走同一条路径——**在副本上改、逐条校验、整批通过才提交**，绝不在实时 project 上边改边试。
 */
import { COLORS, clip, entity, type Cut, type Entity, type Kind, type Project, type Vec3 } from '../model.ts';
import { findAsset } from '../asset-catalog.ts';
import { entityPosition } from '../timeline.ts';
import { removeCameraVisibilityReference } from '../scenes/camera-visibility.ts';
import { removeGroupMember } from '../building/entity-groups.ts';

/** 接口只允许新增这三类对象；crowd 之类需要页面自己的编辑器参数，接口不越界。 */
const ADDABLE: readonly Kind[] = ['actor', 'prop', 'camera'];
/** 缺省资产：人物用老白模，道具用 1 米方块（可缩放成墙、门、家具的占位体），机位用 camera。 */
const DEFAULT_ASSET: Record<string, string> = { actor: 'person', prop: 'shape-box', camera: 'camera' };
/** updateEntity 接受的字段；多传字段直接报错，避免调用方以为改了其实没改。 */
const PATCH_FIELDS = ['kind', 'name', 'asset', 'position', 'rotation', 'scale', 'height', 'camera'];
const CAMERA_FIELDS = ['focal', 'target', 'targetId', 'targetHeight', 'mode', 'aim', 'offset', 'inheritRotation', 'hideWalls', 'hiddenEntityIds'];
/** 构图预设：与页面里“构图”按钮同一张表（只取目标高度与焦距，机位由调用方给）。 */
const FRAMING: Record<string, { targetHeight: (height: number) => number; focal: number }> = {
    '全景': { targetHeight: height => height * .5, focal: 35 },
    '中景': { targetHeight: height => height * .67, focal: 35 },
    '近景': { targetHeight: height => height * .88, focal: 35 },
    '特写': { targetHeight: height => height * .88, focal: 70 },
};
const WALLS = ['north', 'south', 'east', 'west', 'ceiling'];

export interface OperationResult { op: string; id?: string; [key: string]: unknown }

const fail = (message: string): never => { throw new Error(message); };
const isNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const ms = (seconds: number) => Math.round(seconds * 1000);

function vec3(value: unknown, field: string): Vec3 {
    if (!Array.isArray(value) || value.length !== 3 || !value.every(isNumber))
        fail(`${field} 须为三个有限数字组成的数组 [x, y, z]`);
    return [value[0], value[1], value[2]];
}

function scale3(value: unknown, field = 'scale'): Vec3 {
    const parsed = vec3(value, field);
    if (parsed.some(axis => axis <= 0)) fail(`${field} 三个轴须大于 0`);
    return parsed;
}

function numberIn(value: unknown, field: string, min: number, max: number): number {
    if (!isNumber(value) || value < min || value > max) fail(`${field} 须为 ${min}—${max} 之间的有限数字`);
    return value;
}

function finite(value: unknown, field: string): number {
    if (!isNumber(value)) fail(`${field} 须为有限数字`);
    return value;
}

function text(value: unknown, field: string, maxLength = 200): string {
    if (typeof value !== 'string' || !value.trim()) fail(`${field} 须为非空字符串`);
    if (value.length > maxLength) fail(`${field} 不能超过 ${maxLength} 字`);
    return value;
}

function object(value: unknown, field: string): Record<string, unknown> {
    if (!value || typeof value !== 'object' || Array.isArray(value)) fail(`${field} 须为对象`);
    return value as Record<string, unknown>;
}

function findEntity(project: Project, id: unknown): Entity {
    const key = typeof id === 'string' ? id.trim() : '';
    if (!key) fail('缺少对象 id');
    const found = project.entities.find(e => e.id === key);
    if (!found) fail(`对象不存在：${key}`);
    return found!;
}

function heightOf(value: unknown): number { return numberIn(value, 'height', .03, 10); }

/* ---------------------------------------------------------------- 只读快照 */

/** 契约里的时间统一用毫秒；页面内部一律是秒，这里只做边界换算。 */
function pathSnapshot(path: { smooth: boolean; points: readonly { time: number; position: Vec3 }[] } | null | undefined) {
    if (!path) return null;
    return {
        smooth: path.smooth,
        points: path.points.map(point => ({ time_ms: ms(point.time), position: [...point.position] })),
    };
}

function cameraSnapshot(camera: Entity['camera']) {
    if (!camera) return null;
    return {
        aim: camera.aim,
        mode: camera.mode,
        focal: camera.focal,
        target: [...camera.target],
        targetId: camera.targetId,
        targetHeight: camera.targetHeight,
        offset: [...camera.offset],
        inheritRotation: camera.inheritRotation,
        hideWalls: [...camera.hideWalls],
        hiddenEntityIds: [...(camera.hiddenEntityIds ?? [])],
        targetPath: pathSnapshot(camera.targetPath),
    };
}

function entitySnapshot(e: Entity) {
    return {
        id: e.id,
        kind: e.kind,
        name: e.name,
        asset: e.asset,
        visible: e.visible,
        locked: e.locked,
        position: [...e.position],
        rotation: [...e.rotation],
        scale: [...e.scale],
        height: e.height,
        // 路径与动作片段一并给出：主程序算框图要跟页面同一份机位／站位参数。
        path: pathSnapshot(e.path),
        camera: cameraSnapshot(e.camera),
        clips: e.clips.map(c => ({ id: c.id, action: c.action, start_ms: ms(c.start), end_ms: ms(c.end) })),
        ...(e.kind === 'crowd' ? { count: e.count } : {}),
    };
}

export function sceneSnapshot(project: Project, sceneId: string, revision: number) {
    return {
        sceneId,
        revision,
        name: project.name,
        duration_ms: ms(project.duration),
        fps: project.fps,
        aspect: project.aspect,
        room: { ...project.room },
        entities: project.entities.map(entitySnapshot),
        cuts: project.cuts.map(cut => ({ time_ms: ms(cut.time), cameraId: cut.cameraId })),
    };
}

export function cameraList(project: Project) {
    return project.entities.filter(e => e.kind === 'camera').map(e => {
        // 位置取 0 秒的实际机位：有路径的机位在 0 秒也可能不在 authoring 位置上。
        const atZero = entityPosition(e, 0);
        return {
            id: e.id,
            name: e.name,
            position: [atZero.x, atZero.y, atZero.z],
            target: [...(e.camera?.target ?? [0, 0, 0])],
            focal: e.camera?.focal ?? 35,
            mode: e.camera?.mode ?? 'free',
            aim: e.camera?.aim ?? 'target',
            targetId: e.camera?.targetId ?? '',
            targetHeight: e.camera?.targetHeight ?? 1.2,
            hideWalls: [...(e.camera?.hideWalls ?? [])],
            path: pathSnapshot(e.path),
        };
    });
}

/* ---------------------------------------------------------------- 写操作 */

/** 机位配置的局部更新；addEntity 的 camera 字段与 updateEntity 的 camera 字段共用这一条路径。 */
function applyCameraPatch(target: Entity, raw: unknown, project: Project) {
    if (target.kind !== 'camera' || !target.camera) fail('只有机位（kind=camera）可以带 camera 配置');
    const patch = object(raw, 'camera');
    const unknownKeys = Object.keys(patch).filter(key => !CAMERA_FIELDS.includes(key));
    if (unknownKeys.length) fail(`camera 不支持的字段：${unknownKeys.join(' / ')}`);
    const camera = target.camera!;
    if (patch.focal !== undefined) camera.focal = numberIn(patch.focal, 'camera.focal', 8, 300);
    if (patch.target !== undefined) camera.target = vec3(patch.target, 'camera.target');
    if (patch.targetId !== undefined) {
        const id = patch.targetId === null ? '' : String(patch.targetId);
        if (id && !project.entities.some(e => e.id === id && e.kind !== 'camera')) fail(`camera.targetId 指向的对象不存在：${id}`);
        camera.targetId = id;
    }
    if (patch.targetHeight !== undefined) camera.targetHeight = finite(patch.targetHeight, 'camera.targetHeight');
    if (patch.mode !== undefined) {
        if (!['free', 'follow', 'pov'].includes(String(patch.mode))) fail('camera.mode 须为 free / follow / pov');
        camera.mode = patch.mode as typeof camera.mode;
    }
    if (patch.aim !== undefined) {
        if (!['target', 'manual'].includes(String(patch.aim))) fail('camera.aim 须为 target / manual');
        camera.aim = patch.aim as typeof camera.aim;
    }
    if (patch.offset !== undefined) camera.offset = vec3(patch.offset, 'camera.offset');
    if (patch.inheritRotation !== undefined) {
        if (typeof patch.inheritRotation !== 'boolean') fail('camera.inheritRotation 须为 true / false');
        camera.inheritRotation = patch.inheritRotation;
    }
    if (patch.hideWalls !== undefined) {
        if (!Array.isArray(patch.hideWalls) || patch.hideWalls.some(wall => !WALLS.includes(String(wall))))
            fail(`camera.hideWalls 元素可选 ${WALLS.join(' / ')}`);
        camera.hideWalls = patch.hideWalls.map(String) as typeof camera.hideWalls;
    }
    if (patch.hiddenEntityIds !== undefined) {
        if (!Array.isArray(patch.hiddenEntityIds) || patch.hiddenEntityIds.some(id => !project.entities.some(e => e.id === id && e.kind !== 'camera')))
            fail('camera.hiddenEntityIds 须为已存在的非机位对象 id 数组');
        camera.hiddenEntityIds = patch.hiddenEntityIds.map(String);
    }
}

export function addEntityTo(project: Project, raw: unknown): Entity {
    const spec = object(raw, '新增对象参数');
    const kind = spec.kind;
    if (typeof kind !== 'string' || !ADDABLE.includes(kind as Kind)) fail(`kind 只允许 ${ADDABLE.join(' / ')}`);
    const asset = spec.asset === undefined ? DEFAULT_ASSET[kind as string] : text(spec.asset, 'asset', 100);
    // 机位不是资产目录里的白模（它的 asset 固定是 'camera'），只有 actor/prop 才去查资产表。
    const definition = kind === 'camera' ? undefined : findAsset(asset);
    if (kind === 'camera') { if (asset !== 'camera') fail(`机位的 asset 只能是 camera，收到：${asset}`); }
    else if (!definition) fail(`资产不存在：${asset}（可用资产见页面资产面板）`);
    else if (definition.kind !== kind) fail(`资产 ${asset} 不是 ${kind} 类型`);
    const name = spec.name === undefined ? (definition?.name ?? (kind === 'camera' ? '机位' : kind)) : text(spec.name, 'name');
    const position = spec.position === undefined ? [0, 0, 0] as Vec3 : vec3(spec.position, 'position');
    const created = entity(kind as Kind, asset, name, position);
    if (spec.rotation !== undefined) created.rotation = vec3(spec.rotation, 'rotation');
    if (spec.scale !== undefined) created.scale = scale3(spec.scale);
    if (spec.height !== undefined) created.height = heightOf(spec.height);
    if (spec.camera !== undefined) applyCameraPatch(created, spec.camera, project);
    if (created.kind === 'actor') {
        // 与页面“添加资产”一致：给人物一段覆盖整场的待机动作，否则新人物没有任何动作片段。
        created.color = COLORS[project.entities.filter(e => e.kind === 'actor').length % COLORS.length];
        const capabilities = definition!.capabilities;
        if (!capabilities || capabilities.actions.includes('idle')) created.clips = [clip('idle', 0, project.duration)];
    }
    if (project.editorView?.activeFloorId) created.floorId = project.editorView.activeFloorId;
    project.entities.push(created);
    return created;
}

export function updateEntityIn(project: Project, id: unknown, raw: unknown): Entity {
    const target = findEntity(project, id);
    if (target.locked) fail(`对象已锁定，不能通过接口修改：${target.name}`);
    const patch = object(raw, 'patch');
    const unknownKeys = Object.keys(patch).filter(key => !PATCH_FIELDS.includes(key));
    if (unknownKeys.length) fail(`patch 不支持的字段：${unknownKeys.join(' / ')}`);
    if (patch.kind !== undefined && patch.kind !== target.kind) fail('不支持修改对象类型；请删除后重新新增');
    if (patch.asset !== undefined && patch.asset !== target.asset) {
        const asset = text(patch.asset, 'asset', 100);
        if (target.kind === 'camera') { if (asset !== 'camera') fail(`机位的 asset 只能是 camera，收到：${asset}`); }
        else {
            const definition = findAsset(asset);
            if (!definition) fail(`资产不存在：${asset}`);
            if (definition.kind !== target.kind) fail(`资产 ${asset} 不是 ${target.kind} 类型`);
        }
        target.asset = asset;
    }
    if (patch.name !== undefined) target.name = text(patch.name, 'name');
    if (patch.position !== undefined) target.position = vec3(patch.position, 'position');
    if (patch.rotation !== undefined) target.rotation = vec3(patch.rotation, 'rotation');
    if (patch.scale !== undefined) target.scale = scale3(patch.scale);
    if (patch.height !== undefined) target.height = heightOf(patch.height);
    if (patch.camera !== undefined) applyCameraPatch(target, patch.camera, project);
    return target;
}

export function removeEntityFrom(project: Project, id: unknown): OperationResult {
    const target = findEntity(project, id);
    if (target.locked) fail(`对象已锁定，不能通过接口删除：${target.name}`);
    // 手持绑定与结构连接要按引擎里的实时姿态解除，接口不猜：报清楚，让调用方先解绑。
    const blockers: string[] = [];
    if (target.handBinding) blockers.push('该对象自身绑定了手持道具');
    if (target.structureLink) blockers.push('该对象自身参与了结构连接');
    for (const other of project.entities) {
        if (other.id === target.id) continue;
        if (other.handBinding?.actorId === target.id) blockers.push(`${other.name} 手持绑定到它`);
        if (other.structureLink?.parentId === target.id) blockers.push(`${other.name} 结构连接自它`);
    }
    if (blockers.length) fail(`对象仍被引用（${blockers.join('、')}），请先解除后再删除`);
    const replacement = project.entities.find(e => e.kind === 'camera' && e.id !== target.id);
    if (target.kind === 'camera' && !replacement) fail('场景至少需要一台摄影机，不能删除最后一台');
    // 删机位时，切镜表必须整体落到另一台机位上，否则场景立即无效。
    const remapped = project.cuts.filter(cut => cut.cameraId === target.id).map(cut => ms(cut.time));
    for (const cut of project.cuts) if (cut.cameraId === target.id) cut.cameraId = replacement!.id;
    removeCameraVisibilityReference(project, target.id);
    removeGroupMember(project, target.id);
    if (project.editorView?.hiddenEntityIds) project.editorView.hiddenEntityIds = project.editorView.hiddenEntityIds.filter(entityId => entityId !== target.id);
    project.production?.notes.forEach(note => { if (note.actorId === target.id) note.actorId = ''; });
    for (const e of project.entities) {
        if (e.camera?.targetId === target.id) {
            // 被瞄准的目标没了：把机位改成固定瞄准它最后的位置，避免画面突然指向原点。
            e.camera.target = [target.position[0], target.position[1] + e.camera.targetHeight, target.position[2]];
            e.camera.targetId = '';
            e.camera.mode = 'free';
            e.camera.aim = 'target';
        }
        if (e.faceTarget === target.id) { e.faceTarget = ''; e.face = 'fixed'; }
    }
    project.entities = project.entities.filter(e => e.id !== target.id);
    return { op: 'remove', id: target.id, cutsRemapped: remapped };
}

export function setCameraIn(project: Project, id: unknown, raw: unknown): OperationResult {
    const target = findEntity(project, id);
    if (target.kind !== 'camera' || !target.camera) fail(`对象不是机位：${target.id}`);
    if (target.locked) fail(`机位已锁定：${target.name}`);
    const spec = object(raw, '机位参数');
    const unknownKeys = Object.keys(spec).filter(key => !['position', 'target', 'focal', 'framing'].includes(key));
    if (unknownKeys.length) fail(`机位参数不支持的字段：${unknownKeys.join(' / ')}`);
    const camera = target.camera!;
    if (spec.position !== undefined) target.position = vec3(spec.position, 'position');
    if (spec.target !== undefined) {
        camera.target = vec3(spec.target, 'target');
        // 明确给了瞄准点就按静态瞄准处理：跟随目标／目标路径都会覆盖它，留着会“改了没反应”。
        camera.targetId = '';
        camera.targetPath = null;
        camera.aim = 'target';
        camera.mode = 'free';
    }
    if (spec.focal !== undefined) {
        camera.focal = numberIn(spec.focal, 'focal', 8, 300);
        // 焦距动画通道会盖掉静态焦距，删掉才能让调用方给的毫米数真正生效（与页面重新构图一致）。
        if (camera.effects) { delete camera.effects.channels?.focal; camera.effects.dollyZoom = null; }
    }
    if (spec.framing !== undefined) {
        const preset = FRAMING[String(spec.framing)];
        if (!preset) fail(`framing 只支持 ${Object.keys(FRAMING).join(' / ')}`);
        const aimTarget = project.entities.find(e => e.id === camera.targetId) ?? project.entities.find(e => e.kind === 'actor');
        if (!aimTarget) fail('构图需要机位已有目标对象或场景里有人物');
        camera.targetHeight = preset!.targetHeight(aimTarget!.height);
        if (spec.focal === undefined) camera.focal = preset!.focal;
    }
    // 有路径的机位，位置由关键点驱动：setCamera 会清掉路径（等同页面里重新构图），并回报 pathCleared。
    const pathCleared = !!target.path;
    if (pathCleared) target.path = null;
    return { op: 'setCamera', id: target.id, pathCleared, position: [...target.position], target: [...camera.target], focal: camera.focal };
}

export function setCutsIn(project: Project, raw: unknown): OperationResult {
    if (!Array.isArray(raw)) fail('cuts 须为数组 [{time_ms, cameraId}]');
    if (!raw.length) fail('cuts 不能为空：至少需要一个镜头');
    const cameras = new Set(project.entities.filter(e => e.kind === 'camera').map(e => e.id));
    const cuts: Cut[] = raw.map((item, index) => {
        const entry = object(item, `第 ${index + 1} 个切镜`);
        const at = entry.time_ms ?? entry.timeMs ?? (isNumber(entry.time) ? entry.time * 1000 : undefined);
        if (!isNumber(at)) fail(`第 ${index + 1} 个切镜缺少 time_ms`);
        const seconds = at / 1000;
        if (seconds < 0 || seconds >= project.duration) fail(`第 ${index + 1} 个切镜时间须在 0 到 ${project.duration} 秒之间`);
        const cameraId = text(entry.cameraId, `第 ${index + 1} 个切镜的 cameraId`);
        if (!cameras.has(cameraId)) fail(`第 ${index + 1} 个切镜的机位不存在：${cameraId}`);
        return { time: seconds, cameraId };
    });
    if (cuts[0].time !== 0) fail('第一个切镜必须从 0 秒开始');
    for (let i = 1; i < cuts.length; i++) if (cuts[i].time <= cuts[i - 1].time) fail(`第 ${i + 1} 个切镜时间须大于第 ${i} 个`);
    project.cuts = cuts;
    return { op: 'setCuts', count: cuts.length };
}

/** 批量操作里每一条的形状都允许「嵌套在 spec/patch 里」或「平铺在操作对象上」，两种都收。 */
function pick(source: Record<string, unknown>, fields: readonly string[]): Record<string, unknown> {
    const picked: Record<string, unknown> = {};
    for (const field of fields) if (source[field] !== undefined) picked[field] = source[field];
    return picked;
}

export function applyOperation(project: Project, raw: unknown, index: number): OperationResult {
    const operation = object(raw, `第 ${index + 1} 条操作`);
    const op = operation.op;
    switch (op) {
        case 'add':
            return { op: 'add', id: addEntityTo(project, operation.spec ?? pick(operation, ['kind', 'name', 'asset', 'position', 'rotation', 'scale', 'height', 'camera'])).id };
        case 'update': {
            const id = findEntity(project, operation.id).id;
            updateEntityIn(project, id, operation.patch ?? pick(operation, PATCH_FIELDS));
            return { op: 'update', id };
        }
        case 'remove':
            return removeEntityFrom(project, operation.id);
        case 'setCamera': {
            const id = findEntity(project, operation.id).id;
            return { ...setCameraIn(project, id, operation.spec ?? pick(operation, ['position', 'target', 'focal', 'framing'])) };
        }
        case 'setCuts':
            return setCutsIn(project, operation.cuts);
        default:
            return fail(`第 ${index + 1} 条操作的 op 不支持：${String(op)}（可选 add / update / remove / setCamera / setCuts）`);
    }
}

/** 契约要求的「第几条 + 原因」：批量里任何一条失败都带上序号。 */
export function batchFailure(error: unknown, index: number, op: string): string {
    const reason = error instanceof Error ? error.message : String(error ?? '未知错误');
    return `第 ${index + 1} 条（${op}）失败，整批未生效：${reason}`;
}

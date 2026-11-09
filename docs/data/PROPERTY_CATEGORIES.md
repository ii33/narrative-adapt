# 属性分类（schema 11）

分类只决定取值在浏览页显示在哪个方面，不改变事实、事件顺序或逐字证据。`scope` 与分类独立：world 是世界事实，cognition / claim 分别表达人物认知或声称，并指定 `holder_id`。

分类保存在 `property_change.category`，提取草稿里它是必填项：填该节点类型分类表里的一个大类，或显式填「未分类」。移动分类通过 `Operations.property_categorize` 直接编辑所选取值的当前字段；不追加分类日志、撤回记录或别名映射。工程分类词表保存在工程配置，按对象类型提供默认分组，「未分类」是词表末项，也是唯一的人工「暂不归类」选项。

分类只来自显式值，程序不做任何推断：不按属性名猜（「身高」不再自动落到「外貌」），也不按取值猜。已保存的记录里没有 `category` 的（旧工程或手工写入），读取时显示为「未分类」，`category_source` 为 `default_unclassified`；有显式值的记录为 `explicit`。程序不会自动补写分类，也不会批量回填——要归类就在浏览页用 `property_categorize` 手动选，或显式保留未分类。

分类不在当前词表里时，静态检查报 `category_unknown`（warning），不影响取值；完全未分类的记录由 `category_unassigned` 汇总成一条工程级提示（「N 条属性未分类」），没有可跳转的具体对象。

事件和动作通过地点属性引用 `location` 节点时，该属性归入“基本信息”；地点仍是原文证据支持的节点引用，不推断缺失的地点。

关系的 `property_id` 表达具体关系性质，`category=关系` 只是显示分组。物品的持有者语义由定义 `semantic=item_holder` 标识，不能用分类名、认知持有人或含有“归属”的任意词句代替。

所有编辑通过 Operations 的静态校验和轻量动作审计；完整合同见 [数据模型](DATA_MODEL.md) 与 [属性状态合同](PROPERTY_STATE_CONTRACT.md)。

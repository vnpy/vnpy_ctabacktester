# 1.2.0版本

1. vnpy框架4.0版本升级适配

# 1.1.6版本

1. 调整适配PySide6新版本

# 1.1.5版本

1. 修复最长回撤周期显示的问题

# 1.1.4版本

1. 增加i18n国际化支持

# 1.1.3版本

1. 增加对于EWM Sharpe统计指标的支持
2. 修复回测数据为空导致失败后，无法再次启动回测的问题

# 1.1.2版本

1. 如果加载的历史数据为空，则不执行后续回测

# 1.1.1版本

1. 加载策略类时，过滤TargetPosTemplate模板

# 1.1.0版本

1. 增加调用datafeed功能时的日志输出
2. 支持优化时的最大进程数量参数设置

# 1.0.9版本

1. 使用pyqtgraph的GraphicsLayoutWidget来替代已经废弃的GraphicsWindow

# 1.0.8版本

1. 使用zoneinfo替换pytz库
2. 调整安装脚本setup.cfg，添加Python版本限制

# 1.0.7版本

1. 移除反向合约支持

# 1.0.6版本

1. 完善函数和变量的类型声明
2. 对策略下拉框中的策略名称根据首字母排序


# 1.0.5版本

1. 将模块的图标文件信息，改为完整路径字符串
2. 优化组件宽度的调整模式，更好的适应小分辨率屏幕
3. 增加对于Tick历史数据下载的支持
4. 改为使用PySide6风格的信号QtCore.Signal
5. 优化cta_strategy自带策略类的加载逻辑

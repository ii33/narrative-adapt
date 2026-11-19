import { mountLocalization } from './i18n/dom.ts';
import { t } from './i18n/locale.ts';
import { showStartupError } from './ui/startup-error.ts';
import { installPrevisHostApi } from './host/host-api.ts';

// Language and failure recovery must not depend on a working graphics context.
mountLocalization();
// 预演接口的壳尽早挂上：宿主（QtWebEngine）注入的引导脚本只需确认 window.__narrativePrevisHost
// 存在，不必等三维引擎初始化完；接口本体按需 import，不拖慢首屏。
installPrevisHostApi();
document.title = t('导演台');
void import('./main.ts').catch(showStartupError);

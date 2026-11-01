import { Component, type ReactNode } from 'react';
import { Button } from '@/components/ui/button';

interface Props {
  pluginId: string;
  pluginName: string;
  kind: '页面' | '侧栏' | '设置' | '工具栏';
  children: ReactNode;
}

interface State { failed: boolean; attempt: number }

/** A plugin-local boundary. It intentionally keeps exception details out of the UI. */
export class PluginErrorBoundary extends Component<Props, State> {
  state: State = { failed: false, attempt: 0 };

  static getDerivedStateFromError(): Partial<State> { return { failed: true }; }

  componentDidCatch(error: Error) {
    const errorClass = error instanceof TypeError ? 'TypeError'
      : error instanceof RangeError ? 'RangeError'
        : error instanceof ReferenceError ? 'ReferenceError'
          : error instanceof SyntaxError ? 'SyntaxError'
            : error instanceof Error ? 'Error' : 'UnknownError';
    console.error('插件扩展渲染失败', { pluginId: this.props.pluginId, pluginName: this.props.pluginName, kind: this.props.kind, errorClass });
  }

  private retry = () => this.setState((state) => ({ failed: false, attempt: state.attempt + 1 }));

  render() {
    if (this.state.failed) return <div className="rounded-md border p-3 text-sm" role="alert">
      <p>{this.props.pluginName}的{this.props.kind}暂时无法显示。</p>
      <Button className="mt-2" type="button" size="sm" variant="outline" onClick={this.retry}>重试</Button>
      <p className="mt-2 text-xs text-muted-foreground">若代码加载失败，请先保存其它未提交内容，再刷新网页。</p>
    </div>;
    return <div key={this.state.attempt} className="contents">{this.props.children}</div>;
  }
}

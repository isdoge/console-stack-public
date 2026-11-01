import { Component, type ErrorInfo, type ReactNode } from 'react';
import { Button } from '@/components/ui/button';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * 路由级兜底：页面组件渲染崩溃时给用户可操作的错误页，而不是整站白屏。
 * key 挂在路由路径上（见 App.tsx），切页自动重置。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('页面渲染崩溃:', error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 p-8 text-center">
          <p className="text-lg font-semibold">页面出错了</p>
          <p className="max-w-md text-sm text-muted-foreground">
            渲染时发生异常：{this.state.error.message}。可以刷新页面重试；若反复出现，请把上面的报错信息带上有问题的页面地址反馈。
          </p>
          <Button onClick={() => window.location.reload()}>刷新页面</Button>
        </div>
      );
    }
    return this.props.children;
  }
}

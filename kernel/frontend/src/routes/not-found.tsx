import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/button';

/** 404 兜底页：未知路径不再白屏。 */
export function NotFoundRoute() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 p-8 text-center">
      <p className="text-5xl font-semibold tracking-tight text-muted-foreground/60">404</p>
      <p className="text-lg font-semibold">页面不存在</p>
      <p className="max-w-md text-sm text-muted-foreground">
        这个地址没有对应的功能页。可能是链接输错了，或面板已被移除。
      </p>
      <Button asChild>
        <Link to="/">回到总控台</Link>
      </Button>
    </div>
  );
}

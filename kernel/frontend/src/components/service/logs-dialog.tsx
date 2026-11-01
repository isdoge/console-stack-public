import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { toast } from 'sonner';
import { copyText } from '@/lib/utils';

interface Props {
  open: boolean;
  logs: string;
  onOpenChange: (open: boolean) => void;
}

export function LogsDialog({ open, logs, onOpenChange }: Props) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl">
        <DialogHeader>
          <DialogTitle>服务日志</DialogTitle>
        </DialogHeader>
        <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-4 font-mono text-xs">
          {logs || '无日志'}
        </pre>
        <DialogFooter>
          <Button
            variant="outline"
            disabled={!logs}
            onClick={() =>
              copyText(logs)
                .then(() => toast.success('日志已复制'))
                .catch(() => toast.error('复制失败'))
            }
          >
            复制日志
          </Button>
          <Button onClick={() => onOpenChange(false)}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

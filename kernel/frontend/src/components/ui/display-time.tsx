import { displayTime, type TimeOptions } from '@/lib/display-time';

export function DisplayTime({ value, className, ...options }: TimeOptions & {
  value?: string | null;
  className?: string;
}) {
  const time = displayTime(value, options);
  return <time className={className} dateTime={time.dateTime} title={time.title}>{time.text}</time>;
}

import { forwardRef, type MouseEventHandler, type ReactNode } from 'react';
import { NavLink, type NavLinkProps } from 'react-router-dom';
import type { NavigationTarget } from '@/lib/plugins/navigation-types';

type Props = {
  target: Pick<NavigationTarget, 'to' | 'external'>;
  children: ReactNode;
  className?: NavLinkProps['className'];
  title?: string;
  onClick?: MouseEventHandler<HTMLAnchorElement>;
};

export const NavigationLink = forwardRef<HTMLAnchorElement, Props>(function NavigationLink(
  { target, children, className, ...props }, ref
) {
  if (target.external) {
    const classes = typeof className === 'function'
      ? className({ isActive: false, isPending: false, isTransitioning: false }) : className;
    return <a {...props} ref={ref} className={classes} href={target.to} target="_blank" rel="noopener noreferrer">{children}</a>;
  }
  return <NavLink {...props} ref={ref} to={target.to} className={className}>{children}</NavLink>;
});

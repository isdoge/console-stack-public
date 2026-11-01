export default function ThemeBackground({ themeId, children }) {
  return (
    <div
      data-theme={themeId}
      className="ui-shell min-h-screen text-white font-sans antialiased relative overflow-x-hidden"
    >
      {themeId === 'glass' && (
        <div className="fixed inset-0 pointer-events-none overflow-hidden z-0" aria-hidden="true">
          <div className="ui-particle w-72 h-72 -top-24 -left-24 opacity-50" style={{ animationDelay: '0s' }} />
          <div className="ui-particle w-56 h-56 top-[15%] right-[8%] opacity-40" style={{ animationDelay: '-4s' }} />
          <div className="ui-particle w-40 h-40 top-[55%] left-[12%] opacity-35" style={{ animationDelay: '-8s' }} />
          <div className="ui-particle w-64 h-64 -bottom-20 right-[25%] opacity-45" style={{ animationDelay: '-12s' }} />
          <div className="ui-particle w-28 h-28 top-[40%] left-[55%] opacity-30" style={{ animationDelay: '-6s' }} />
          <div className="ui-particle w-48 h-48 top-[8%] left-[40%] opacity-25" style={{ animationDelay: '-16s' }} />
          <div className="ui-particle w-20 h-20 bottom-[20%] left-[5%] opacity-40" style={{ animationDelay: '-10s' }} />
        </div>
      )}
      {themeId === 'glow' && (
        <div className="fixed inset-0 pointer-events-none overflow-hidden z-0" aria-hidden="true">
          <div className="ui-glow-orb ui-glow-orb-cyan w-[28rem] h-[28rem] -top-40 -left-40" />
          <div className="ui-glow-orb ui-glow-orb-purple w-[24rem] h-[24rem] top-[20%] -right-32" style={{ animationDelay: '-6s' }} />
          <div className="ui-glow-orb ui-glow-orb-magenta w-[20rem] h-[20rem] bottom-[5%] left-[30%]" style={{ animationDelay: '-10s' }} />
          <div className="ui-glow-orb ui-glow-orb-cyan w-64 h-64 top-[60%] right-[20%] opacity-60" style={{ animationDelay: '-14s' }} />
          <div className="ui-glow-orb ui-glow-orb-purple w-80 h-80 bottom-[30%] -left-20 opacity-50" style={{ animationDelay: '-8s' }} />
        </div>
      )}
      <div className="relative z-10">{children}</div>
    </div>
  );
}

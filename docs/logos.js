/* Logo concepts for the splash screen and favicon. Each returns an inline SVG.
   Classes drive the splash animation:
     .k  strokes drawn first (ink)      .r  red stroke drawn second
     .g  faint guide, fades in          .dot red point that pops in last          */
window.LOGOS = {
  // A falling line that has just started to turn up: the screen's whole thesis.
  rebound: {
    name: "Rebound",
    note: "A price line that falls hard, then turns up. Sold off, but still growing.",
    svg: `<svg class="lg" viewBox="0 0 64 64" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path class="g" d="M6 14H58" stroke-dasharray="2 4"/>
      <path class="k" pathLength="1" d="M6 14 L19 27 L28 21 L42 49"/>
      <path class="r" pathLength="1" d="M42 49 L57 37"/>
      <circle class="dot" cx="42" cy="49" r="3.4"/></svg>`,
  },
  // A gauge half gone: the 50%-below-the-high rule.
  half: {
    name: "Half",
    note: "A ring with half of it gone. The 50%-off-the-high rule as a mark.",
    svg: `<svg class="lg" viewBox="0 0 64 64" fill="none" stroke-linecap="round" aria-hidden="true">
      <circle class="g" cx="32" cy="32" r="22" stroke-dasharray="2 4"/>
      <path class="k" pathLength="1" d="M32 10 A22 22 0 0 1 32 54"/>
      <path class="r" pathLength="1" d="M32 54 A22 22 0 0 1 14.6 45.4"/>
      <circle class="dot" cx="14.6" cy="45.4" r="3.4"/></svg>`,
  },
  // Price against the 52-week high, as two bars.
  bars: {
    name: "Ledger bars",
    note: "Two bars, the 52-week high and today's price, with a tick at the halfway mark.",
    svg: `<svg class="lg" viewBox="0 0 64 64" fill="none" stroke-linecap="round" aria-hidden="true">
      <path class="g" d="M32 12V52" stroke-dasharray="2 3"/>
      <path class="k" pathLength="1" d="M8 24H56" stroke-width="6"/>
      <path class="r" pathLength="1" d="M8 40H27" stroke-width="6"/>
      <circle class="dot" cx="27" cy="40" r="0"/></svg>`,
  },
  // A W drawn as a price chart whose last leg only partly recovers.
  monogram: {
    name: "W chart",
    note: "A W for 'wounded', drawn as a chart whose last leg only half recovers.",
    svg: `<svg class="lg" viewBox="0 0 64 64" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path class="g" d="M6 15H58" stroke-dasharray="2 4"/>
      <path class="k" pathLength="1" d="M8 15 L20 49 L32 24 L44 49"/>
      <path class="r" pathLength="1" d="M44 49 L53 31"/>
      <circle class="dot" cx="53" cy="31" r="3.4"/></svg>`,
  },
};

/* Favicon from a logo, with fixed colours (tabs don't follow page tokens). */
window.logoFavicon = key => {
  const svg = window.LOGOS[key].svg
    .replace('class="lg"', 'xmlns="http://www.w3.org/2000/svg" stroke-width="4"')
    .replace(/class="g"[^/]*\/>/, "/>")
    .replace(/class="k"/g, 'stroke="#16181d"')
    .replace(/class="r"/g, 'stroke="#c8102e"')
    .replace(/class="dot"/g, 'fill="#c8102e"');
  return "data:image/svg+xml," + encodeURIComponent(svg);
};

// Hot Dog Man. After 15 seconds without a tap, key, scroll, or mouse move, he leans in from the right edge of the
// screen and stares. Any activity sends him back. He stays away while a dialog is open (the scanner can sit still
// for a long time), while the tab is hidden, and for people who ask for reduced motion he just appears, without the
// slow lean.
//
// He's drawn badly on purpose: every line wobbles.

const IDLE_MS = 15000;

const SVG = `
<svg viewBox="0 0 320 640" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <defs>
    <!-- shakes every line, like it was drawn with the wrong hand on a bus -->
    <filter id="hd-wobble" x="-20%" y="-20%" width="140%" height="140%">
      <feTurbulence type="fractalNoise" baseFrequency="0.04" numOctaves="2" seed="7"/>
      <feDisplacementMap in="SourceGraphic" scale="10"/>
    </filter>
  </defs>
  <g stroke="#2a1a10" stroke-linecap="round" stroke-linejoin="round" filter="url(#hd-wobble)">
    <!-- the bun, lumpy and squashed, bitten at the bottom -->
    <path d="M92 70 C40 90 28 190 34 300 C38 400 30 500 58 590 C66 612 96 622 120 610 C126 596 140 600 150 612
             C170 626 214 620 236 596 C262 560 270 470 266 380 C262 280 276 170 248 104 C230 60 150 44 92 70 Z"
          fill="#d9a35b" stroke-width="7"/>
    <path d="M60 160 C58 150 66 146 70 152 M232 230 C238 222 246 228 240 236 M70 470 C64 462 74 456 78 464"
          fill="none" stroke="#a8743a" stroke-width="4"/>
    <!-- the sausage, too big for the bun, bulging out -->
    <path d="M118 40 C70 52 66 160 74 270 C80 380 70 470 88 560 C96 598 140 606 168 592 C204 574 214 520 212 440
             C210 360 228 250 220 150 C214 80 172 28 118 40 Z"
          fill="#b8604f" stroke-width="8"/>
    <path d="M96 120 C92 200 98 300 96 380 M196 160 C200 240 192 330 198 420" fill="none" stroke="#e8a090"
          stroke-width="6" opacity="0.7"/>
    <!-- grease spots and blotches -->
    <g fill="#8e3f30" stroke="none" opacity="0.55">
      <ellipse cx="190" cy="200" rx="9" ry="6"/><ellipse cx="88" cy="520" rx="12" ry="8"/>
      <ellipse cx="150" cy="540" rx="7" ry="10"/><ellipse cx="200" cy="350" rx="6" ry="9"/>
    </g>
    <!-- ketchup, smeared on with a thumb -->
    <path d="M120 470 C140 448 170 500 200 486 C206 500 176 520 150 510 C130 504 126 520 112 508 Z"
          fill="#a3121a" stroke-width="3"/>
    <path d="M200 486 C210 500 206 516 212 530" fill="none" stroke="#a3121a" stroke-width="6"/>
    <!-- grill marks, crooked -->
    <path d="M90 300 L150 286 M92 338 L196 316 M98 376 L180 362 M96 420 L204 398" fill="none" stroke="#6b1f14"
          stroke-width="6"/>
    <!-- mustard, slopped on and dripping -->
    <path d="M110 250 C130 240 120 270 146 262 C172 254 150 290 178 282 C200 276 186 312 160 318 C136 324 160 344
             130 352 C104 360 128 380 110 392 C96 402 120 420 104 436"
          fill="none" stroke="#f2c314" stroke-width="13"/>
    <path d="M104 436 C100 452 104 466 98 474" fill="none" stroke="#f2c314" stroke-width="9"/>
    <circle cx="97" cy="482" r="6" fill="#f2c314" stroke-width="3"/>
    <!-- a stain of some kind -->
    <path d="M168 470 C182 462 196 476 188 488 C182 500 162 498 160 486 C158 478 162 474 168 470 Z" fill="#7a3a1c"
          stroke-width="3" opacity="0.8"/>
    <!-- greasy hair, four strands -->
    <path d="M110 46 C104 20 124 14 120 -4 M134 40 C140 16 132 6 144 -8 M152 42 C170 26 160 8 178 2
             M124 44 C116 32 94 34 90 18" fill="none" stroke-width="5"/>
    <!-- eyes: one huge and bloodshot, one small and droopy, both too close together -->
    <g class="hd-eyes">
      <ellipse cx="114" cy="130" rx="34" ry="38" fill="#fffbe8" stroke-width="6" transform="rotate(-8 114 130)"/>
      <path d="M86 118 L100 124 L96 130 M84 140 L98 136 M92 158 L104 148 L100 142 M138 104 L128 116 L132 122
               M142 150 L130 142 M118 96 L116 110 M110 166 L114 152 M146 126 L132 130 L128 126"
            fill="none" stroke="#d2202a" stroke-width="2.5"/>
      <circle class="hd-pupil" cx="100" cy="134" r="10" fill="#1a0f08" stroke="none"/>
      <circle cx="96" cy="129" r="3" fill="#fff" stroke="none"/>
      <ellipse cx="176" cy="138" rx="18" ry="16" fill="#f3e9a8" stroke-width="5"/>
      <circle class="hd-pupil" cx="168" cy="142" r="6" fill="#1a0f08" stroke="none"/>
      <!-- the droopy lid on the small eye -->
      <path d="M156 132 C164 118 190 120 196 134 C186 130 170 130 156 132 Z" fill="#a83a26" stroke-width="5"/>
    </g>
    <!-- bags under the eyes -->
    <path d="M84 168 C100 184 134 182 146 166 M160 158 C170 170 190 168 198 156" fill="none" stroke="#6e2a20"
          stroke-width="4"/>
    <path d="M90 178 C106 192 130 190 140 178" fill="none" stroke="#6e2a20" stroke-width="3" opacity="0.6"/>
    <!-- unibrow, uneven -->
    <path d="M78 88 C98 76 120 92 140 84 C156 78 172 98 196 108" fill="none" stroke-width="9"/>
    <!-- nose, lumpy -->
    <path d="M140 160 C126 178 128 198 146 200 C160 202 162 190 154 184" fill="#b33a26" stroke-width="5"/>
    <!-- a wide, wet grin with not enough teeth -->
    <path d="M86 214 C106 248 168 252 204 210 C194 238 168 262 136 262 C108 262 92 244 86 214 Z" fill="#3b0d0a"
          stroke-width="6"/>
    <path d="M104 228 L108 242 L116 230 Z M128 236 L130 252 L140 238 Z M176 228 L174 244 L186 232 Z" fill="#f4ecc6"
          stroke-width="2.5"/>
    <!-- a tongue, lolling -->
    <path d="M150 248 C170 252 178 276 170 290 C162 302 146 296 148 280 C150 268 144 258 150 248 Z" fill="#d9707a"
          stroke-width="4"/>
    <path d="M160 262 C162 274 160 282 158 288" fill="none" stroke="#a3404c" stroke-width="2.5"/>
    <!-- drool, two strings of it -->
    <path d="M120 258 C122 280 116 300 122 326 C124 336 118 344 122 352" fill="none" stroke="#bfe6f2" stroke-width="5"/>
    <circle cx="122" cy="358" r="5" fill="#bfe6f2" stroke-width="2"/>
    <path d="M168 296 C170 312 166 322 170 334" fill="none" stroke="#bfe6f2" stroke-width="4"/>
    <!-- double chin -->
    <path d="M100 292 C120 312 168 312 190 288" fill="none" stroke="#6e2a20" stroke-width="5"/>
    <!-- a hairy mole -->
    <circle cx="196" cy="236" r="8" fill="#4a2414" stroke-width="2"/>
    <path d="M196 230 C200 218 212 216 214 206 M198 234 C210 230 218 236 226 230 M192 232 C186 222 190 214 184 206"
          fill="none" stroke-width="2"/>
    <!-- belly hair -->
    <path d="M140 400 C136 392 144 388 140 380 M156 410 C160 400 152 396 158 388 M146 424 C140 418 148 412 144 404
             M164 428 C170 420 162 414 168 406 M132 418 C126 410 134 404 128 398"
          fill="none" stroke-width="2.5"/>
    <!-- a fly, interested -->
    <g transform="translate(236 60)">
      <ellipse cx="0" cy="0" rx="6" ry="4" fill="#1a1a1a" stroke="none"/>
      <ellipse cx="-4" cy="-6" rx="5" ry="3" fill="#dfe8ee" stroke-width="1.5" opacity="0.8"/>
      <ellipse cx="4" cy="-6" rx="5" ry="3" fill="#dfe8ee" stroke-width="1.5" opacity="0.8"/>
      <path d="M-12 8 C-8 4 -10 0 -16 -2 M10 12 C14 6 20 8 22 2" fill="none" stroke="#555" stroke-width="1.5"
            stroke-dasharray="2 3"/>
    </g>
    <!-- stubble -->
    <g fill="#2a1a10" stroke="none">
      <circle cx="96" cy="262" r="2"/><circle cx="110" cy="272" r="2"/><circle cx="124" cy="278" r="2"/>
      <circle cx="160" cy="276" r="2"/><circle cx="176" cy="268" r="2"/><circle cx="190" cy="256" r="2"/>
      <circle cx="104" cy="284" r="2"/><circle cx="168" cy="286" r="2"/><circle cx="82" cy="246" r="2"/>
    </g>
    <!-- sweat -->
    <path d="M206 92 C200 106 204 116 212 116 C220 116 222 106 206 92 Z" fill="#bfe6f2" stroke-width="3"/>
    <path d="M60 196 C56 206 58 214 64 214 C70 214 72 206 60 196 Z" fill="#bfe6f2" stroke-width="3"/>
    <!-- a skinny arm reaching toward you, with too many knuckles -->
    <path d="M86 322 C80 312 88 308 84 300 M90 340 C82 350 90 356 84 364 M80 330 C70 326 72 318 64 316"
          fill="none" stroke-width="2.5"/>
    <path d="M86 330 C40 336 10 352 -30 344 C-52 340 -70 350 -84 342" fill="none" stroke="#b8604f"
          stroke-width="14"/>
    <path d="M86 330 C40 336 10 352 -30 344 C-52 340 -70 350 -84 342" fill="none" stroke-width="4" opacity="0.6"/>
    <path d="M-84 342 C-104 326 -112 318 -126 322 M-84 342 C-110 340 -122 338 -136 346 M-84 342 C-104 354 -114 362
             -128 362 M-82 346 C-90 362 -96 372 -108 378"
          fill="none" stroke="#b8604f" stroke-width="9"/>
    <g fill="#3a2a10" stroke="none">
      <circle cx="-126" cy="322" r="4"/><circle cx="-136" cy="346" r="4"/><circle cx="-128" cy="362" r="4"/>
      <circle cx="-108" cy="378" r="4"/>
    </g>
  </g>
</svg>`;

export function startHotDogMan() {
  const man = document.createElement("div");
  man.className = "hotdog-man";
  man.innerHTML = SVG;
  document.body.append(man);

  let timer = 0;
  const away = () => {
    man.classList.remove("leaning");
    clearTimeout(timer);
    timer = setTimeout(lean, IDLE_MS);
  };
  function lean() {
    if (document.hidden || document.querySelector("dialog[open]")) return away(); // try again later
    man.classList.add("leaning");
  }

  for (const type of ["pointerdown", "pointermove", "keydown", "wheel", "scroll", "touchstart"]) {
    addEventListener(type, away, { passive: true, capture: true });
  }
  document.addEventListener("visibilitychange", away);
  away();
}

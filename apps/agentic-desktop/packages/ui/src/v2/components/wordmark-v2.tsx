import { createUniqueId, type ComponentProps } from "solid-js"

export function WordmarkV2(props: Pick<ComponentProps<"svg">, "class">) {
  const filter = createUniqueId()
  const mask = createUniqueId()
  const maskGradient = createUniqueId()

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 720.002 129.001"
      fill="none"
      preserveAspectRatio="none"
      classList={{ [props.class ?? ""]: !!props.class }}
    >
      <g opacity="0.16" filter={`url(#${filter})`} mask={`url(#${mask})`}>
        {/* a */}
        <path
          opacity="0.7"
          d="M18.4615 18.4286H73.8462V110.5714H0.0000V55.2857H55.3846V36.8571H18.4615ZM55.3846 73.7143H18.4615V92.1429H55.3846Z"
          fill="currentColor"
        />
        {/* g */}
        <path
          opacity="0.7"
          d="M92.3077 18.4286H166.1538V129.0000H92.3077V92.1429H110.7692V110.5714H147.6923V73.7143H92.3077ZM147.6923 36.8571H110.7692V55.2857H147.6923Z"
          fill="currentColor"
        />
        {/* e */}
        <path
          opacity="0.7"
          d="M258.463 73.7154H203.079V92.144H258.463V110.573H184.617V18.4297H258.463V73.7154ZM203.079 55.2868H240.002V36.8583H203.079V55.2868Z"
          fill="currentColor"
        />
        {/* n */}
        <path
          opacity="0.7"
          d="M332.306 36.8583H295.383V110.573H276.922V18.4297H332.306V36.8583ZM350.768 110.573H332.306V36.8583H350.768V110.573Z"
          fill="currentColor"
        />
        {/* t */}
        <path
          opacity="0.7"
          d="M387.6923 0.0000H406.1538V18.4286H424.6154V36.8571H406.1538V92.1429H443.0769V110.5714H387.6923V36.8571H369.2308V18.4286H387.6923Z"
          fill="currentColor"
        />
        {/* space at slot 6 */}
        {/* i */}
        <path
          opacity="0.7"
          d="M572.3077 0.0000H590.7692V18.4286H572.3077ZM572.3077 36.8571H590.7692V110.5714H572.3077Z"
          fill="currentColor"
        />
        {/* c */}
        <path
          opacity="0.7"
          d="M720.0000 36.8571H664.6154V92.1429H720.0000V110.5714H646.1538V18.4286H720.0000V36.8571Z"
          fill="currentColor"
        />
      </g>
      <defs>
        <mask id={mask} maskUnits="userSpaceOnUse" x="0" y="0" width="720" height="129">
          <rect width="720" height="129" fill={`url(#${maskGradient})`} />
        </mask>
        <linearGradient id={maskGradient} x1="360" y1="0" x2="360" y2="112" gradientUnits="userSpaceOnUse">
          <stop stop-color="white" stop-opacity="0.7" />
          <stop offset="1" stop-color="white" stop-opacity="0" />
        </linearGradient>
        <filter
          id={filter}
          x="0"
          y="0"
          width="720.002"
          height="130.001"
          filterUnits="userSpaceOnUse"
          color-interpolation-filters="sRGB"
        >
          <feFlood flood-opacity="0" result="BackgroundImageFix" />
          <feBlend mode="normal" in="SourceGraphic" in2="BackgroundImageFix" result="shape" />
          <feColorMatrix
            in="SourceAlpha"
            type="matrix"
            values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0"
            result="hardAlpha"
          />
          <feOffset dy="1" />
          <feGaussianBlur stdDeviation="1" />
          <feComposite in2="hardAlpha" operator="arithmetic" k2="-1" k3="1" />
          <feColorMatrix type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0" />
          <feBlend mode="normal" in2="shape" result="effect1_innerShadow_4938_16028" />
        </filter>
      </defs>
    </svg>
  )
}

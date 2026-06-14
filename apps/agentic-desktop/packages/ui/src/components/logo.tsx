import { type ComponentProps } from "solid-js"

export const Mark = (props: { class?: string }) => {
  return (
    <svg
      data-component="logo-mark"
      classList={{ [props.class ?? ""]: !!props.class }}
      viewBox="0 0 20 20"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path
        d="M3 5.5C3 3.567 4.567 2 6.5 2H14C15.657 2 17 3.343 17 5V12.5C17 14.433 15.433 16 13.5 16H6C4.343 16 3 14.657 3 13V5.5Z"
        fill="var(--icon-weak-base)"
      />
      <path
        d="M8.143 5H14L11.857 9H14.5L7.714 17L9.286 11.75H6L8.143 5Z"
        fill="var(--icon-strong-base)"
      />
      <path
        d="M6 1V0H8V1H12V0H14V1C16.761 1 19 3.239 19 6H20V8H19V12H20V14H19C19 16.761 16.761 19 14 19V20H12V19H8V20H6V19C3.239 19 1 16.761 1 14H0V12H1V8H0V6H1C1 3.239 3.239 1 6 1ZM6 3C4.343 3 3 4.343 3 6V14C3 15.657 4.343 17 6 17H14C15.657 17 17 15.657 17 14V6C17 4.343 15.657 3 14 3H6Z"
        fill="var(--icon-base)"
      />
    </svg>
  )
}

export const Splash = (props: Pick<ComponentProps<"svg">, "ref" | "class">) => {
  return (
    <svg
      ref={props.ref}
      data-component="logo-splash"
      classList={{ [props.class ?? ""]: !!props.class }}
      viewBox="0 0 100 100"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path
        d="M17 26C17 16.611 24.611 9 34 9H70C78.284 9 85 15.716 85 24V60C85 69.389 77.389 77 68 77H32C23.716 77 17 70.284 17 62V26Z"
        fill="var(--icon-weak-base)"
      />
      <path d="M41 25H70L59 47H72L34 91L43 62H27L41 25Z" fill="var(--icon-strong-base)" />
      <path
        d="M30 4V0H40V4H60V0H70V4C84.359 4 96 15.641 96 30H100V40H96V60H100V70H96C96 84.359 84.359 96 70 96V100H60V96H40V100H30V96C15.641 96 4 84.359 4 70H0V60H4V40H0V30H4C4 15.641 15.641 4 30 4ZM30 14C21.163 14 14 21.163 14 30V70C14 78.837 21.163 86 30 86H70C78.837 86 86 78.837 86 70V30C86 21.163 78.837 14 70 14H30Z"
        fill="var(--icon-base)"
      />
    </svg>
  )
}

export const Logo = (props: { class?: string }) => {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 260 48"
      fill="none"
      classList={{ [props.class ?? ""]: !!props.class }}
    >
      <g transform="translate(0 6)">
        <path
          d="M3 5.5C3 3.567 4.567 2 6.5 2H14C15.657 2 17 3.343 17 5V12.5C17 14.433 15.433 16 13.5 16H6C4.343 16 3 14.657 3 13V5.5Z"
          fill="var(--icon-weak-base)"
        />
        <path d="M8.143 5H14L11.857 9H14.5L7.714 17L9.286 11.75H6L8.143 5Z" fill="var(--icon-strong-base)" />
        <path
          d="M6 1V0H8V1H12V0H14V1C16.761 1 19 3.239 19 6H20V8H19V12H20V14H19C19 16.761 16.761 19 14 19V20H12V19H8V20H6V19C3.239 19 1 16.761 1 14H0V12H1V8H0V6H1C1 3.239 3.239 1 6 1ZM6 3C4.343 3 3 4.343 3 6V14C3 15.657 4.343 17 6 17H14C15.657 17 17 15.657 17 14V6C17 4.343 15.657 3 14 3H6Z"
          fill="var(--icon-base)"
        />
      </g>
      <text
        x="30"
        y="31"
        fill="var(--icon-strong-base)"
        font-family="Inter, ui-sans-serif, system-ui, sans-serif"
        font-size="24"
        font-weight="760"
        letter-spacing="0"
      >
        AgentIC
      </text>
      <text
        x="130"
        y="31"
        fill="var(--icon-base)"
        font-family="Inter, ui-sans-serif, system-ui, sans-serif"
        font-size="13"
        font-weight="650"
        letter-spacing="0"
      >
        Silicon
      </text>
    </svg>
  )
}

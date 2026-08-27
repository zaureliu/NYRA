# Live2D readiness

Live2D não é dependência da V3. O contrato futuro deve oferecer:

hair_back, hair_front, hair_left, hair_right, identity_strand, face, eyebrows, eyes, iris, pupils, mouth, neck, torso, arms, jacket, neural_link e symbol.

Parâmetros mínimos: blink, eye_open, eye_x, eye_y, mouth_open, mouth_form, breath, head_x, head_y e body_x. Estados do EventBus continuam sendo a fonte, e o provider FutureLive2DRenderer implementará a mesma interface do LayeredRenderer.

Limitação atual: os PNGs não estão segmentados nessas camadas. Os overlays V3 são placeholders funcionais para boca/olhos/Neural Link, não um rig Live2D.

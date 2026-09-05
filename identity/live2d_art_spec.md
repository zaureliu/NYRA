# NYRA Live2D Art Specification

Esta especificação é apenas orientação para um modelo externo administrado no VTube Studio. Nenhuma arte ou modelo resultante é embutido no runtime NYRA.

Canvas recomendado: 4096×4096, bust/portrait, margem segura ao redor do cabelo e ombros. Preserve rosto, cabelo violeta/ameixa escuro, mecha teal discreta, olhos teal, roupa grafite e Neural Link ciano.

Estrutura PSD:

```text
FACE/FaceBase Nose FaceShadow EarL EarR
BROWS/BrowL BrowR
EYES/EyeWhiteL EyeWhiteR IrisL IrisR PupilL PupilR UpperLashL UpperLashR LowerLashL LowerLashR HighlightL HighlightR
MOUTH/MouthLine UpperLip LowerLip MouthInside Teeth Tongue
HAIR/HairBack HairFront BangL BangCenter BangR SideHairL01 SideHairL02 SideHairR01 SideHairR02 HairTipL HairTipR IdentityStrand Highlights
BODY/Neck ShoulderL ShoulderR Torso Jacket ClothingDetails
NEURAL_LINK/NeuralLinkL NeuralLinkR NeuralGlowL NeuralGlowR
```

Partes ocultas precisam ser desenhadas sob cabelo/pálpebras/boca. Highlights e glows ficam separados. Não adicionar pernas. Não copiar penteado, rosto, acessórios ou proporções de personagens existentes.

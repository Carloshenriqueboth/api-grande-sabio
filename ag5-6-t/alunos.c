#include <stdio.h>
#include <string.h>

#define ARQUIVO "alunos.txt"
#define TAM_NOME 100

int main(void) {
    char nome[TAM_NOME];
    float nota1, nota2, media;
    FILE *arquivo;

    /* ---------- Gravação no arquivo ---------- */
    printf("Nome do aluno: ");
    if (fgets(nome, sizeof(nome), stdin) == NULL) {
        printf("Erro ao ler o nome.\n");
        return 1;
    }
    nome[strcspn(nome, "\n")] = '\0';   /* remove a quebra de linha */

    printf("Nota 1: ");
    if (scanf("%f", &nota1) != 1) {
        printf("Nota invalida.\n");
        return 1;
    }

    printf("Nota 2: ");
    if (scanf("%f", &nota2) != 1) {
        printf("Nota invalida.\n");
        return 1;
    }

    media = (nota1 + nota2) / 2.0f;

    /* modo "a": acrescenta o aluno sem apagar os registros anteriores */
    arquivo = fopen(ARQUIVO, "a");
    if (arquivo == NULL) {
        printf("Erro ao abrir o arquivo para escrita.\n");
        return 1;
    }

    /* formato: nome;nota1;nota2;media */
    fprintf(arquivo, "%s;%.2f;%.2f;%.2f\n", nome, nota1, nota2, media);
    fclose(arquivo);

    printf("\nDados salvos em %s com sucesso!\n\n", ARQUIVO);

    /* ---------- Leitura e exibição ---------- */
    arquivo = fopen(ARQUIVO, "r");
    if (arquivo == NULL) {
        printf("Erro ao abrir o arquivo para leitura.\n");
        return 1;
    }

    printf("%-30s %8s %8s %8s\n", "ALUNO", "NOTA 1", "NOTA 2", "MEDIA");
    printf("------------------------------------------------------------\n");

    while (fscanf(arquivo, " %99[^;];%f;%f;%f", nome, &nota1, &nota2, &media) == 4) {
        printf("%-30s %8.2f %8.2f %8.2f\n", nome, nota1, nota2, media);
    }

    fclose(arquivo);
    return 0;
}
